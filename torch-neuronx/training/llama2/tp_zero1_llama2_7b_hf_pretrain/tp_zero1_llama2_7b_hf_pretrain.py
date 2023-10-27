# coding=utf-8
# Copyright (c) 2019 NVIDIA CORPORATION. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Modifications Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import math
import torch
import sys
import time
import argparse
import json
import queue
from typing import Any, Dict, List
from datetime import datetime, timezone
from collections import namedtuple
import torch_xla
import torch_xla.core.xla_model as xm
from torch.utils.data.dataloader import DataLoader
from torch.utils.data import DistributedSampler
import torch_xla.distributed.parallel_loader as pl
import torch.distributed as dist
import torch_xla.distributed.xla_multiprocessing as xmp
import torch_xla.distributed.xla_backend
import numpy as np
from transformers import (
    AdamW,
    default_data_collator,
    set_seed,
    LlamaConfig,
)
from transformers.optimization import get_linear_schedule_with_warmup

import copy
from torch.utils.tensorboard import SummaryWriter
import inspect
import requests
from neuronx_distributed.parallel_layers import parallel_state, layers, grads, checkpointing
from neuronx_distributed.utils.model_utils import move_model_to_device
from neuronx_distributed.parallel_layers.grads import bucket_allreduce_gradients
from neuronx_distributed.parallel_layers.utils import is_pjrt_device
import datasets

from neuronx_distributed.optimizer import NeuronZero1Optimizer
from adamw_fp32_optim_params import AdamW_FP32OptimParams
from modeling_llama2_nxd import LlamaForCausalLMNxD

# For PT autocast.
torch.cuda.is_bf16_supported = lambda: True

# Workaround for NaNs seen with transformers version >= 4.21.0
# https://github.com/aws-neuron/aws-neuron-sdk/issues/593
import transformers.modeling_utils as modeling_utils

if os.environ.get("XLA_USE_BF16") or os.environ.get("XLA_DOWNCAST_BF16"):
    modeling_utils.get_parameter_dtype = lambda x: torch.bfloat16

datetime_str = str(datetime.now())
results = {
    "inference_success": 1
}


Metric = namedtuple("Metric", ["name", "value", "units", "additional_data"])


class TrainingMetrics:
    """
    This class is used for logging metrics to a json file. One can provide a 
    dictionary of metrics that needs to be stored, and it wpuld get 
    written to the file.
    Arguments:
        json_file: File used for logging. If no file exists, new file would be created.
    """
    def __init__(self, json_file):
        self.json_file = json_file

    def read_modify_write_file(self, data, key: str = "metrics") -> None:
        """
        data (dict of training parameters or list of metrics): Data to update in the file.
        key (str): the dictionary key under which data is to be recorded
        """
        result_dict = {}
        print(f"Writing data to the provided results file: {self.json_file}")
        if os.path.exists(self.json_file):
            with open(self.json_file) as json_file:
                result_dict = json.loads(json_file.read()) or result_dict
        print(f"Updating with {key} data: {data}")
        if result_dict:
            try:
                # handle internal named entity if present
                results = result_dict[next(iter(result_dict))]
            except Exception:
                results = result_dict
            current = results.get(key)
            if not current:
                results[key] = data
            else:
                if isinstance(current, list):
                    current.extend(data)
                elif isinstance(current, dict):
                    current.update(data)
        else:
            result_dict["results"] = {key: data}
        with open(self.json_file, "w") as json_file:
            json.dump(result_dict, json_file)

    def store_metrics(self, metrics: List[Metric]) -> None:
        """
        Writes collected metrics to the file.
        """
        data = [
            {
                "MetricName": metric.name,
                "MeasuredValue": metric.value,
                "Units": metric.units,
                "Timestamp": datetime.now(timezone.utc).isoformat(),
                "AdditionalData": metric.additional_data,
            }
            for metric in metrics
        ]
        self.update(data=data, key="metrics")

    def store_parameters(self, parameters: Dict[str, Any]) -> None:
        """
        Writes specified model and configuration parameters to the file.
        """
        self.update(data=parameters, key="parameters")

    def update(self, **kwargs: Any) -> None:
        """
        Write specified data to the output file.
        """
        self.read_modify_write_file(**kwargs)


class Throughput:
    """
    Used to calculate the throughput over a moving window. It records the step time
    between two calls and uses that time to calculate the throughput.
    """
    def __init__(
        self, batch_size, world_size, grad_accum_usteps, moving_avg_window_size=10, logging_interval=1
    ):
        self.seqs_per_iteration = batch_size * world_size * grad_accum_usteps*logging_interval
        self.moving_avg_window_size = math.ceil(moving_avg_window_size/logging_interval)
        self.moving_avg_window = queue.Queue()
        self.window_time = 0
        self.start_time = time.time()

    def get_throughput(self):
        step_time = time.time() - self.start_time
        self.start_time += step_time
        self.window_time += step_time
        self.moving_avg_window.put(step_time)
        window_size = self.moving_avg_window.qsize()
        if window_size > self.moving_avg_window_size:
            self.window_time -= self.moving_avg_window.get()
            window_size -= 1
        throughput = window_size * self.seqs_per_iteration / self.window_time
        return throughput


class Logger:
    def __init__(self, args, world_size, model_dtype):
        xla = "torch_xla" in sys.modules
        self.throughputs = []
        dtype_short = model_dtype.replace("torch.", "")
        self.tb = SummaryWriter(
            os.path.join(
                args.output_dir,
                f"neuron_tblogs_{time.strftime('%m%d%y_%H%M')}"
                f"_{dtype_short}"
                f"_w{world_size}"
                f"_lr{args.lr}"
                f"_bs{args.batch_size}"
                f"_acc{args.grad_accum_usteps}"
                f"_warmup{args.warmup_steps}"
                f"_max{args.max_steps}"
                f"_xla{xla}"
                f"_{self.get_instance_type()}",
            )
        )
        self.tb.add_text(
            "script", "```\n" + inspect.getsource(sys.modules[__name__]) + "\n```", 0
        )
        self.golden_steploss = []
        golden = "golden_steploss.txt"
        if os.path.exists(golden):
            with open(golden, "r") as f:
                self.golden_steploss = [float(i) for i in f]
            print(
                f"Read {len(self.golden_steploss)} golden step loss values from {golden}"
            )

    def get_instance_type(self):
        try:
            token = requests.put(
                "http://169.254.169.254/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            )
            data = requests.get(
                "http://169.254.169.254/latest/meta-data/instance-type",
                headers={"X-aws-ec2-metadata-token": token.text},
            )
            return data.text
        except:
            return os.environ.get("HOSTNAME", "unknown")

    def log(self, epoch, step, step_loss, learning_rate, throughput, grad_norm=None):
        time_now = time.asctime()
        grad_norm_msg = f"grad-norm : {grad_norm}" if grad_norm else ""
        print(
            f"LOG {time_now} - ({epoch}, {step}) step_loss : {step_loss:.4f} "
            f"learning_rate : {learning_rate:.2e} throughput : {throughput:.2f} "
            f"{grad_norm_msg}",
            flush=True,
        )
        self.tb.add_scalar("step loss", step_loss, step)
        self.tb.add_scalar("learning rate", learning_rate, step)
        self.tb.add_scalar("throughput", throughput, step)
        if grad_norm:
            self.tb.add_scalar("grad-norm", grad_norm, step)
        self.throughputs.append(throughput)
        if not os.environ.get("NEURON_EXTRACT_GRAPHS_ONLY", None):
            step_0start = step - 1
            if step_0start < len(self.golden_steploss) and step_0start >= 0:
                np.testing.assert_allclose(
                    step_loss, self.golden_steploss[step_0start], rtol=2.3e-1
                )


# Workaround because python functions are not picklable
class WorkerInitObj(object):
    def __init__(self, seed):
        self.seed = seed

    def __call__(self, id):
        set_seed(self.seed)

def create_pretraining_dataset(
    data_dir, mini_batch_size, worker_init
):
    train_data = datasets.load_from_disk(os.path.expanduser(data_dir))
    train_sampler = DistributedSampler(
        train_data,
        num_replicas=parallel_state.get_data_parallel_size(),
        rank=parallel_state.get_data_parallel_rank(),
        shuffle=False,
        drop_last=True,
    )
    train_dataloader = DataLoader(
        train_data,
        collate_fn=default_data_collator,
        sampler=train_sampler,
        batch_size=mini_batch_size,
        num_workers=0,
        worker_init_fn=worker_init,
        drop_last=True,
        pin_memory=True,
    )
    return train_dataloader

def get_model(flags):
    model_path, seq_len = flags.model_path, flags.seq_len
    config = LlamaConfig.from_pretrained(model_path)
    config.use_cache = False
    config.max_position_embeddings = max(config.max_position_embeddings, seq_len)
    if flags.num_layers > 0:
        config.num_hidden_layers = flags.num_layers
    if flags.sequence_parallel_enabled:
        config.sequence_parallel_enabled = True
    if flags.selective_checkpoint_enabled:
        config.selective_checkpoint_enabled = True
    xm.master_print(config)
    model = LlamaForCausalLMNxD(config)
    xm.master_print(model)
    return model

def get_dtype(model) -> str:
    """
    Reference: https://pytorch.org/xla/release/1.12/index.html#xla-tensors-and-bfloat16
    """
    if "XLA_USE_BF16" in os.environ:
        return "torch.bfloat16"
    if "XLA_DOWNCAST_BF16" in os.environ:
        if "torch.float" in str(model.dtype):
            return "torch.bfloat16"
        if "torch.double" in str(model.dtype):
            return "torch.float32"
    return str(model.dtype)    

def allreduce_sequence_parallel_gradients(optimizer):
    """ All-reduce layernorm parameters across model parallel nodes when sequence parallelism is used.
        Modified from megatron-lm:
        https://gitlab-master.nvidia.com/ADLR/megatron-lm/-/blob/3f91f09bb2ab32f9904b47f46f19d2fc3f518ed8/megatron/training.py#L425
    """
    from neuronx_distributed.parallel_layers.mappings import reduce_from_tensor_model_parallel_region
    grads = []
    for param_group in optimizer.__getstate__()['param_groups']:
        for group, params in param_group.items():
            if group == 'params':
                for p in params:
                    if isinstance(p, torch.Tensor) and p.grad is not None:
                        sequence_parallel_param = getattr(p, 'sequence_parallel_enabled', False)
                        if sequence_parallel_param:
                            grads.append(p.grad.data)
    for grad in grads:
        # sum v.s. average: sum
        reduce_from_tensor_model_parallel_region(grad)

def train_llama(flags):
    # Initialize the model parallelism with the tp degree
    parallel_state.initialize_model_parallel(tensor_model_parallel_size=flags.tensor_parallel_size)
    world_size = parallel_state.get_data_parallel_size()
    is_root = xm.is_master_ordinal(local=False)
    extract_graphs_only = os.environ.get("NEURON_EXTRACT_GRAPHS_ONLY", None)
    set_seed(flags.seed)
    worker_init = WorkerInitObj(flags.seed)
    device = xm.xla_device()

    model = get_model(flags)
    # Moving the model to device using NxD's API. This takes care of preserving the 
    # tp/sp attributes
    move_model_to_device(model, device)
    model.train()

    model_dtype = get_dtype(model)
    running_loss = torch.zeros(1, dtype=torch.double).to(device)

    param_optimizer = list(model.named_parameters())
    no_decay = ["bias", "LayerNorm"]  # gamma/beta are in LayerNorm.weight

    optimizer_grouped_parameters = [
        {
            "params": [
                p for n, p in param_optimizer if not any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.01,
        },
        {
            "params": [
                p for n, p in param_optimizer if any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]

    if flags.use_mix_precision:
        optimizer_cls = AdamW_FP32OptimParams
    else:
        optimizer_cls = AdamW

    if flags.use_zero_1:
        optimizer = NeuronZero1Optimizer(
            optimizer_grouped_parameters,
            optimizer_cls,
            lr=flags.lr,
            pin_layout=False,
            sharding_groups=parallel_state.get_data_parallel_group(as_list=True),
            grad_norm_groups=parallel_state.get_tensor_model_parallel_group(as_list=True),
        )
    else:
        optimizer = optimizer_cls(optimizer_grouped_parameters, flags.lr)
    optimizer.zero_grad()

    if is_root:
        if not os.path.exists(flags.output_dir):
            os.makedirs(flags.output_dir, exist_ok=True)
        if not extract_graphs_only:
            logger = Logger(flags, world_size, model_dtype)
        metric_writer = TrainingMetrics(flags.metrics_file)
        throughput = Throughput(
            flags.batch_size, world_size, flags.grad_accum_usteps, logging_interval=args.logging_interval
        )
        print("--------TRAINING CONFIG----------")
        print(flags)
        print("--------MODEL CONFIG----------")
        print(model.config)
        print("---------------------------------")
        metric_writer.store_parameters(
            {
                "Model": model.name_or_path,
                "Model configuration": str(model.config),
                "World size": xm.xrt_world_size(),
                "Data parallel degree": world_size,
                "Batch size": flags.batch_size,
                "Total steps": flags.steps_this_run,
                "Seed": flags.seed,
                "Optimizer": str(optimizer),
                "Data type": model_dtype,
                "Gradient accumulation microsteps": flags.grad_accum_usteps,
                "Warmup steps": flags.warmup_steps,
                "Dataset": os.path.basename(os.path.normpath(flags.data_dir)),
                "Environment variables": {
                    variable: value
                    for variable, value in os.environ.items()
                    if variable.startswith("NEURON") or variable.startswith("XLA")
                },
            }
        )

    def train_loop_fn(
        model, optimizer, train_loader, epoch, global_step, training_ustep, running_loss, use_zero_1
    ):
        for _, data in enumerate(train_loader):
            training_ustep += 1
            input_ids = data["input_ids"]
            attention_mask = data["attention_mask"]
            labels = data["labels"]
            outputs = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                labels=labels,
            )
            loss = outputs.loss / flags.grad_accum_usteps
            loss.backward()
            running_loss += loss.detach()

            if training_ustep % flags.grad_accum_usteps == 0:
                xm.mark_step()
                # loss averaging
                running_loss_div = running_loss / world_size
                # Collecting loss across all data-parallel ranks
                running_loss_reduced = xm.all_reduce(
                    xm.REDUCE_SUM,
                    running_loss_div,
                    groups=parallel_state.get_data_parallel_group(as_list=True),
                )
                running_loss_reduced_detached = running_loss_reduced.detach()
                running_loss.zero_()

                # For sequence-parallel, we have to explicitly all-reduce the layernorm
                # gradients.
                allreduce_sequence_parallel_gradients(optimizer)
                if not use_zero_1:
                    # all-reduce and then clip. Order matters.
                    if parallel_state.get_data_parallel_size() > 1:
                        bucket_allreduce_gradients(xm._fetch_gradients(optimizer))
                    max_grad_norm = 1.0
                    grads.clip_grad_norm(
                        model.parameters(), max_grad_norm
                    )  # Gradient clipping is not in AdamW anymore
                optimizer.step()
                total_norm_detach = 0.0
                with torch.no_grad():
                    total_norm = torch.zeros(1, device=device)
                    if flags.print_grad_norm and is_root:
                        for p in model.parameters():
                            param_norm_sq = torch.square(p.grad).sum()
                            total_norm += param_norm_sq
                        total_norm = torch.sqrt(total_norm)
                        total_norm_detach = total_norm.detach()

                optimizer.zero_grad()
                scheduler.step()
                global_step += 1

                def _print_logs(running_loss_reduced_detached, total_norm):
                    if is_root and not extract_graphs_only:
                        total_norm_cpu = None
                        if flags.print_grad_norm:
                            total_norm_cpu = total_norm.cpu().item()
                        # NOTE: The running_loss is the loss of the global_step
                        logger.log(
                            epoch,
                            global_step,
                            running_loss_reduced_detached.cpu().item(),
                            optimizer.param_groups[0]["lr"],
                            throughput.get_throughput(),
                            total_norm_cpu,
                        )

                if global_step % flags.logging_interval == 0:
                    # Printing the loss inside the step closure. This won't block
                    # the tracing for next step. Also, we are logging every N steps.
                    # This is done to reduce the overhead of copying tensors to CPU.
                    # Tensor copy is expensive since it prevents the next step to start.
                    xm.add_step_closure(
                        _print_logs, (running_loss_reduced_detached, total_norm_detach)
                    )
                if global_step >= flags.steps_this_run:
                    # NOTE: Prevent runtime "Call to recv failed : Broken pipe" issue
                    xm.mark_step()
                    break

        return (
            global_step,
            training_ustep,
            running_loss,
            running_loss_reduced_detached.cpu().item(),
        )

    scheduler_state_dict = None

    if flags.resume_ckpt:
        state_dict = checkpointing.load(flags.output_dir, model)
        optimizer.load_state_dict(state_dict["optimizer"])
        global_step = state_dict["global_step"]
        epoch = state_dict["epoch"]
        scheduler_state_dict = state_dict["scheduler"]
    else:
        global_step = 0
        epoch = 0

    train_start = time.time()
    training_ustep = 0
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=flags.warmup_steps,
        num_training_steps=flags.max_steps,
        last_epoch=epoch if scheduler_state_dict else -1,
    )

    if scheduler_state_dict:
        scheduler.load_state_dict(scheduler_state_dict)

    assert os.path.exists(
        os.path.expanduser(flags.data_dir)
    ), "ERROR: Data directory {} doesn't exist!".format(flags.data_dir)

    mini_batch_size = flags.batch_size
    train_dataloader = create_pretraining_dataset(
        flags.data_dir, mini_batch_size, worker_init
    )
    # We wrap the dataloader with MpDeviceLoader. This dataloader should take
    # care of copying the tensors to device and also inserting the mark_step at
    # iteration end.
    train_device_loader = pl.MpDeviceLoader(train_dataloader, device)

    while True:
        xm.master_print(
            "Epoch {} begin {}".format(epoch, time.asctime()),
            flush=True,
        )

        global_step, training_ustep, running_loss, final_loss = train_loop_fn(
            model,
            optimizer,
            train_device_loader,
            epoch,
            global_step,
            training_ustep,
            running_loss,
            flags.use_zero_1,
        )

        if is_root and not extract_graphs_only:
            final_time = time.time()
            time_diff = final_time - train_start
            print(
                "Epoch {} step {} end {} loss {} perf {} seq/sec (at train microstep {} time {} from beginning time {})".format(
                    epoch,
                    global_step,
                    time.asctime(),
                    final_loss,
                    logger.throughputs[-1],
                    training_ustep,
                    final_time,
                    train_start,
                ),
                flush=True,
            )
            additional_data = {
                "Epoch": epoch,
                "Global step": global_step,
                "Microstep": training_ustep,
            }
            metric_data = [
                Metric("Loss", final_loss, "", additional_data),
                Metric(
                    "Throughput", logger.throughputs[-1], "seq/s", additional_data
                ),
            ]
            metric_writer.store_metrics(metric_data)

        if global_step >= flags.steps_this_run:
            if is_root and not extract_graphs_only:
                # record aggregate & final statistics in the metrics file
                additional_data = {
                    "Epoch": epoch,
                    "Global step": global_step,
                    "Microstep": training_ustep,
                }
                average_throughput = round(
                    sum(logger.throughputs) / len(logger.throughputs), 4
                )
                metric_data = [
                    Metric("Final loss", final_loss, "", additional_data),
                    Metric(
                        "Time to train",
                        round(time_diff / 60, 4),
                        "minutes",
                        additional_data,
                    ),
                    Metric(
                        "Average throughput",
                        average_throughput,
                        "seq/s",
                        additional_data,
                    ),
                    Metric(
                        "Peak throughput",
                        max(logger.throughputs),
                        "seq/s",
                        additional_data,
                    ),
                ]
                metric_writer.store_metrics(metric_data)
            # TODO may incur HOST OOM
            state_dict = {
               "model": model.state_dict(),
               "global_step": global_step,
               "epoch": epoch,
               "scheduler": scheduler.state_dict()
            }
            # Note: We are not saving the optimizer using the checkpoint.save API.
            # This is because the Zero1 Optimizer is sharded across data-parallel ranks
            # and hence all DP ranks need to save. checkpoint.save API only saves from 
            # DP rank=0
            checkpointing.save(state_dict, flags.output_dir)
            optimizer.save_sharded_state_dict(flags.output_dir)
            return

        epoch += 1


def _mp_fn(index, flags):
    torch.set_default_tensor_type("torch.FloatTensor")
    train_llama(flags)
    xm.rendezvous("_mp_fn finished")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_path",
        type=str,
        help="Model weight and config path.",
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        help="Pre-tokenized dataset directory.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./output",
        help="Directory for checkpoints and logs.",
    )
    parser.add_argument(
        "--metrics_file",
        type=str,
        default="results.json",
        help="training metrics results file",
    )
    parser.add_argument("--batch_size", type=int, default=8, help="Worker batch size.")
    parser.add_argument(
        "--max_steps",
        type=int,
        help="Maximum total accumulation-steps to run.",
    )
    parser.add_argument(
        "--steps_this_run",
        type=int,
        help="Exit early at <value> steps and not go to max_steps. -1 to mean no early exit.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=12349,
        help="Random seed. Worker seed is this value + worker rank.",
    )
    parser.add_argument("--lr", type=float, default=4e-4, help="Learning rate.")
    parser.add_argument(
        "--warmup_steps",
        type=int,
        default=2000,
        help="Number of warmup accumulation-steps for learning rate .",
    )
    parser.add_argument(
        "--grad_accum_usteps",
        type=int,
        default=64,
        help="Gradient accumulation micro-steps (an accumulation-step has <value> micro-steps.",
    )
    parser.add_argument(
        "--print_grad_norm",
        default=False,
        action="store_true",
        help="Whether to print grad norm",
    )
    parser.add_argument(
        "--resume_ckpt",
        action="store_true",
        help="Resume from checkpoint at resume_step."
    )
    parser.add_argument(
        "--tensor_parallel_size",
        default=2,
        type=int,
        help="Tensor parallel size"
    )
    parser.add_argument(
        "--seq_len",
        default=2048,
        type=int,
        help="Sequence length"
    )
    parser.add_argument(
        "--use_mix_precision", action="store_true", help="Use mix precision."
    )
    parser.add_argument(
        "--use_zero_1", action="store_true", help="Use ZeRO-1."
    )
    parser.add_argument(
        "--num_layers",
        type=int,
        default=-1,
        help="Override number of layers for this LLaMA model",
    )
    parser.add_argument(
        "--sequence_parallel_enabled",
        default=False,
        action="store_true",
        help="Enable sequence parallel",
    )
    parser.add_argument(
        "--selective_checkpoint_enabled",
        default=False,
        action="store_true",
        help="Enable selective checkpoint",
    )
    parser.add_argument(
        "--logging_interval",
        default=1,
        type=int,
        help="Enable selective checkpoint",
    )

    args = parser.parse_args(sys.argv[1:])

    if args.steps_this_run < 0:
        args.steps_this_run = args.max_steps

    os.environ["NEURON_RT_STOCHASTIC_ROUNDING_EN"] = "1"
    if args.use_mix_precision:
        os.environ["XLA_DOWNCAST_BF16"]="1"
    else:
        os.environ["XLA_USE_BF16"]="1"


    # WORLD_SIZE is set by torchrun
    if os.environ.get("WORLD_SIZE"):
        if is_pjrt_device():
            import torch_xla.experimental.pjrt_backend
            dist.init_process_group("xla", init_method="pjrt://")
        else:
            dist.init_process_group("xla") 
        _mp_fn(0, args)
    else:
        xmp.spawn(_mp_fn, args=(args,))
