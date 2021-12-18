from dl_training.core import Base
from datasets import ArrayDataset, DataItem, MultiModalDataset
from datasets import OpenBHB, SubOpenBHB
import torch
from torch.utils.data import SequentialSampler
from dl_training.augmentation import *
from dl_training.transforms import Crop, HardNormalization
import torchvision.transforms as transforms
import bisect
from tqdm import tqdm
from typing import Dict, List
import numpy as np
from itertools import compress


class DA_Module(object):

    def __init__(self):
        self.compose_transforms = Transformer()

        self.compose_transforms.register(flip, probability=0.5)
        self.compose_transforms.register(add_blur, probability=0.5, sigma=(0.1, 1))
        self.compose_transforms.register(add_noise, sigma=(0.1, 1), probability=0.5)
        self.compose_transforms.register(cutout, probability=0.5, patch_size=32, inplace=False)
        self.compose_transforms.register(Crop((96, 96, 96), "random", resize=True), probability=0.5)

    def __call__(self, x):
        return self.compose_transforms(x)


class SimCLROpenBHB(OpenBHB):
    def __getitem__(self, idx: int):
        np.random.seed()
        x1, y1 = super().__getitem__(idx)
        x2, y1 = super().__getitem__(idx)
        return np.stack((x1, x2), axis=0), y1


class SimCLRSubOpenBHB(SubOpenBHB):
    def __getitem__(self, idx: int):
        np.random.seed()
        x1, y1 = super().__getitem__(idx)
        x2, y1 = super().__getitem__(idx)
        return np.stack((x1, x2), axis=0), y1


class SimCLR(Base):
    def get_output_pairs(self, inputs, **kwargs):
        """
        :param inputs: torch.Tensor
        :return: pair (z_i, z_j) where z_i and z_j have the same structure as inputs
        """
        z_i = self.model(inputs[:, 0, :].to(self.device))
        z_j = self.model(inputs[:, 1, :].to(self.device))
        return z_i, z_j


    def update_metrics(self, values, nb_batch, logits=None, target=None, validation=False, **kwargs):
        if logits is not None and target is not None:
            for name, metric in self.metrics.items():
                if validation:
                    name = name + " on validation set"
                if name not in values:
                    values[name] = 0
                values[name] += float(metric(logits, target)) / nb_batch

    def train(self, loader, visualizer=None, fold=None, epoch=None, **kwargs):
        """ Train the model on the trained data.

        Parameters
        ----------
        loader: a pytorch Dataloader

        Returns
        -------
        loss: float
            the value of the loss function.
        values: dict
            the values of the metrics.
        """

        self.model.train()
        nb_batch = len(loader)
        pbar = tqdm(total=nb_batch, desc="Mini-Batch")

        values = {}
        iteration = 0

        losses = []
        for dataitem in loader:
            pbar.update()
            inputs = dataitem.inputs
            labels = dataitem.labels.to(self.device) if dataitem.labels is not None else None
            self.optimizer.zero_grad()
            (z_i, z_j) = self.get_output_pairs(inputs, **kwargs)
            if labels is not None:
                batch_loss, *args = self.loss(z_i, z_j, labels)
            else:
                batch_loss, *args = self.loss(z_i, z_j)

            batch_loss.backward()
            self.optimizer.step()

            aux_losses = (self.loss.get_aux_losses() if hasattr(self.loss, 'get_aux_losses') else dict())
            for name, aux_loss in aux_losses.items():
                if name not in values:
                    values[name] = 0
                values[name] += float(aux_loss) / nb_batch

            losses.append(float(batch_loss))
            if iteration % 40 == 0:
                if visualizer is not None:
                    visualizer.refresh_current_metrics()
                    if hasattr(self.model, "get_current_visuals"):
                        visuals = self.model.get_current_visuals()
                        visualizer.display_images(visuals, ncols=3)
            iteration += 1

            self.update_metrics(values, nb_batch, *args, **kwargs)

        loss = np.mean(losses)

        pbar.close()
        return loss, values

    def features_avg_test(self, loader, M=10, **kwargs):
        """ Evaluate the model at test time using the feature averaging strategy as described in
        Improving Transformation Invariance in Contrastive Representation Learning, ICLR 2021, A. Foster

        :param
        loader: a pytorch Dataset
            the data loader.
        M: int, default 10
            nb of times we sample t~T such that we transform a sample x -> z := f(t(x))
        :returns
            y: array-like dims (n_samples, M, ...) where ... is the dims of the network's output
            the predicted data.
            y_true: array-like dims (n_samples, M,  ...) where ... is the dims of the network's output
            the true data
        """
        M = int(M)

        assert M//2 == M/2.0, "Nb of feature vectors averaged should be odd"

        if not isinstance(loader.sampler, SequentialSampler):
            raise ValueError("The dataloader must use the sequential sampler (avoid random_sampler option)")

        print(loader.dataset, flush=True)


        self.model.eval()
        nb_batch = len(loader)
        pbar = tqdm(total=nb_batch*(M//2), desc="Mini-Batch")

        with torch.no_grad():
            y, y_true = [], []
            for _ in range(M//2):
                current_y, current_y_true = [[], []], []
                for dataitem in loader:
                    pbar.update()
                    if dataitem.labels is not None:
                        current_y_true.extend(dataitem.labels.cpu().detach().numpy())
                    (z_i, z_j) = self.get_output_pairs(dataitem.inputs, **kwargs)
                    current_y[0].extend(z_i.cpu().detach().numpy())
                    current_y[1].extend(z_j.cpu().detach().numpy())
                y.extend(current_y)
                y_true.extend([current_y_true, current_y_true])
            pbar.close()
            # Final dim: y [M, n_samples, ...] and y_true [M, n_samples, ...]
            # Sanity check
            assert np.all(np.array(y_true)[0,:] == np.array(y_true)), "Wrong iteration order through the dataloader"
            y = np.array(y).swapaxes(0, 1)
            y_true = np.array(y_true).swapaxes(0, 1)

        return y, y_true


    def test(self, loader, with_visuals=False, **kwargs):
        """ Evaluate the model on the validation data. The test is done in a usual way for a supervised task.

        Parameter
        ---------
        loader: a pytorch Dataset
            the data loader.

        Returns
        -------
        y: array-like
            the predicted data.
        y_true: array-like
            the true data
        X: array_like
            the input data
        loss: float
            the value of the loss function.
        values: dict
            the values of the metrics.
        """

        self.model.eval()
        nb_batch = len(loader)
        pbar = tqdm(total=nb_batch, desc="Mini-Batch")
        loss = 0
        values = {}
        visuals = []
        y, y_true, X = [], [], []

        with torch.no_grad():
            for dataitem in loader:
                pbar.update()
                inputs = dataitem.inputs
                labels = dataitem.labels.to(self.device) if dataitem.labels is not None else None

                (z_i, z_j) = self.get_output_pairs(inputs, **kwargs)
                if with_visuals:
                    visuals.append(self.model.get_current_visuals())

                if labels is not None:
                    batch_loss, *args = self.loss(z_i, z_j, labels)
                else:
                    batch_loss, *args = self.loss(z_i, z_j)

                loss += float(batch_loss) / nb_batch
                #y.extend(logits.detach().cpu().numpy())
                #y_true.extend(target.detach().cpu().numpy())

                # eventually appends the inputs to X
                #for i in inputs:
                #    X.extend(i.cpu().detach().numpy())

                # Now computes the metrics with (y, y_true)
                self.update_metrics(values, nb_batch, *args, validation=True, **kwargs)

                aux_losses = (self.loss.get_aux_losses() if hasattr(self.loss, 'get_aux_losses') else dict())
                for name, aux_loss in aux_losses.items():
                    name += " on validation set"
                    if name not in values:
                        values[name] = 0
                    values[name] += aux_loss / nb_batch

        pbar.close()

        if len(visuals) > 0:
            visuals = np.concatenate(visuals, axis=0)

        if with_visuals:
            return y, y_true, X, loss, values, visuals

        return y, y_true, X, loss, values