import argparse
import os

import horovod.torch as hvd
import ray
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data.distributed
from filelock import FileLock
from ray.util.sgd.v2 import Trainer
from torchvision import datasets, transforms


def metric_average(val, name):
    tensor = torch.tensor(val)
    avg_tensor = hvd.allreduce(tensor, name=name)
    return avg_tensor.item()


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return F.log_softmax(x)


class TrainClass:
    def __init__(self, config):
        self.model = Net()
        self.use_cuda = config.get("use_cuda", False)

        data_dir = config.get("data_dir", None)
        seed = config.get("seed", 42)
        batch_size = config.get("batch_size", 64)
        use_adasum = config.get("use_adasum", False)
        lr = config.get("lr", 0.01)
        momentum = config.get("momentum", 0.5)

        # Horovod: initialize library.
        hvd.init()
        torch.manual_seed(seed)

        if self.use_cuda:
            # Horovod: pin GPU to local rank.
            torch.cuda.set_device(hvd.local_rank())
            torch.cuda.manual_seed(seed)

        # Horovod: limit # of CPU threads to be used per worker.
        torch.set_num_threads(1)

        kwargs = {"num_workers": 1, "pin_memory": True} if self.use_cuda \
            else {}
        data_dir = data_dir or "~/data"
        with FileLock(os.path.expanduser("~/.horovod_lock")):
            train_dataset = \
                datasets.MNIST(data_dir, train=True, download=True,
                               transform=transforms.Compose([
                                   transforms.ToTensor(),
                                   transforms.Normalize((0.1307,), (0.3081,))
                               ]))
        # Horovod: use DistributedSampler to partition the training data.
        self.train_sampler = torch.utils.data.distributed.DistributedSampler(
            train_dataset, num_replicas=hvd.size(), rank=hvd.rank())
        self.train_loader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=batch_size,
            sampler=self.train_sampler,
            **kwargs)

        model = Net()

        # By default, Adasum doesn't need scaling up learning rate.
        lr_scaler = hvd.size() if not use_adasum else 1

        if self.use_cuda:
            # Move model to GPU.
            model.cuda()
            # If using GPU Adasum allreduce, scale learning rate by local_size.
            if use_adasum and hvd.nccl_built():
                lr_scaler = hvd.local_size()

        # Horovod: scale learning rate by lr_scaler.
        optimizer = optim.SGD(
            model.parameters(), lr=lr * lr_scaler, momentum=momentum)

        # Horovod: wrap optimizer with DistributedOptimizer.
        self.optimizer = hvd.DistributedOptimizer(
            optimizer,
            named_parameters=model.named_parameters(),
            op=hvd.Adasum if use_adasum else hvd.Average)

    def train(self, epoch):
        self.model.train()
        # Horovod: set epoch to sampler for shuffling.
        self.train_sampler.set_epoch(epoch)
        num_batches = len(self.train_loader)
        for batch_idx, (data, target) in enumerate(self.train_loader):
            if self.use_cuda:
                data, target = data.cuda(), target.cuda()
            self.optimizer.zero_grad()
            output = self.model(data)
            loss = F.nll_loss(output, target)
            loss.backward()
            self.optimizer.step()
            if batch_idx == num_batches - 1:
                return loss.item()


def main(num_workers, use_gpu, num_epochs, config):
    trainer = Trainer("horovod", use_gpu=use_gpu, num_workers=num_workers)
    trainer.start()
    workers = trainer.to_worker_group(TrainClass, config)
    results = []
    for epoch in range(num_epochs):
        loss = ray.get([w.train.remote(epoch=epoch) for w in workers])
        results.append(loss)
    trainer.shutdown()
    print(results)


if __name__ == "__main__":
    # Training settings
    parser = argparse.ArgumentParser(
        description="PyTorch MNIST Example",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)")
    parser.add_argument(
        "--num-epochs",
        type=int,
        default=5,
        metavar="N",
        help="number of epochs to train (default: 5)")
    parser.add_argument(
        "--lr",
        type=float,
        default=0.01,
        metavar="LR",
        help="learning rate (default: 0.01)")
    parser.add_argument(
        "--momentum",
        type=float,
        default=0.5,
        metavar="M",
        help="SGD momentum (default: 0.5)")
    parser.add_argument(
        "--use-gpu",
        action="store_true",
        default=False,
        help="enables CUDA training")
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        metavar="S",
        help="random seed (default: 42)")
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status")
    parser.add_argument(
        "--use-adasum",
        action="store_true",
        default=False,
        help="use adasum algorithm to do reduction")
    parser.add_argument(
        "--num-workers",
        type=int,
        default=2,
        help="Number of Ray workers to use for training.")
    parser.add_argument(
        "--data-dir",
        help="location of the training dataset in the local filesystem ("
        "will be downloaded if needed)")
    parser.add_argument(
        "--address",
        required=False,
        type=str,
        default=None,
        help="Address of Ray cluster.")

    args = parser.parse_args()

    if args.address:
        ray.init(args.address)
    else:
        ray.init()

    use_cuda = args.use_gpu if args.use_gpu is not None else False

    kwargs = {
        "data_dir": args.data_dir,
        "seed": args.seed,
        "use_cuda": use_cuda,
        "batch_size": args.batch_size,
        "use_adasum": args.use_adasum if args.use_adasum else False,
        "lr": args.lr,
        "momentum": args.momentum,
        "log_interval": args.log_interval
    }

    main(
        num_workers=args.num_workers,
        use_gpu=use_cuda,
        num_epochs=args.num_epochs,
        config=kwargs)