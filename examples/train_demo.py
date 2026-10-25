"""Tiny GPU demo; copy metrics.py beside this file before snapshotting examples/."""
import argparse
import os
import time
from pathlib import Path

import torch
from metrics import write_metrics

parser = argparse.ArgumentParser()
parser.add_argument("--steps", type=int, default=20)
args = parser.parse_args()
if not torch.cuda.is_available():
    raise SystemExit("当前环境没有可用 CUDA GPU")
model = torch.nn.Linear(16, 1).cuda()
optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
x = torch.randn(128, 16, device="cuda")
y = x.sum(dim=1, keepdim=True)
for step in range(args.steps):
    optimizer.zero_grad()
    loss = (model(x) - y).square().mean()
    loss.backward()
    optimizer.step()
    write_metrics(step, {"loss": loss.item()}, split="train")
    print(f"step={step} loss={loss.item():.6f}", flush=True)
    time.sleep(0.2)
output = Path(os.environ["CAMPUS_OUTPUT_DIR"])
torch.save(model.state_dict(), output / "model.pt")
