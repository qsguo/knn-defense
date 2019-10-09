'''Implement gradient-based attack on DkNN with L-inf constraint'''

import logging

import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim

INFTY = 1e20


class DKNNLinfAttack(object):
    """
    Implement gradient-based attack on Deep k-Nearest Neigbhor that uses
    L-2 distance as a metric. Perturbation is constrained in an L-inf ball.
    """

    def __call__(self, dknn, x_orig, label, guide_layer='relu1', m=100,
                 binary_search_steps=5, max_iterations=500,
                 learning_rate=1e-2, initial_const=1, abort_early=True,
                 max_linf=None, random_start=False, guide_mode=1):
        """
        Parameters
        ----------
        dknn : DKNN object
            DkNN (defined in lin/dknn.py) that we want to attack
        x_orig : torch.tensor
            tensor of the original samples to attack. Does not need to require
            gradients, shape is (num_samples, ) + input_shape
        label : torch.tensor
            tensor of the label corresponding to x_orig
        guide_layer : str. optional
            layer name in which we want to find guide samples. Default is
            'relu1'
        m : int, optional
            number of guide samples. Default is 100
        binary_search_step : int, optional
            number of steps for binary search on the norm penalty constant.
            Default is 5
        max_iterations : int, optional
            number of optimization steps (per one binary search). Default is
            500
        learning_rate : float , optional
            step size or learning rate for the optimizer. Default is 1e-2
        initial_const : float, optional
            a number the norm penalty constant should be initialized to.
            Default is 1
        abort_early : bool, optional
            whether or not to abort the optimization early (before reaching
            max_iterations) if the objective does not improve from the past
            (max_iterations // 10) steps. Default is True
        max_linf : float, optional
            use to bound the L-inf norm of the attacks (addition to L-2 norm
            penalty). Set to None to not use this option. Default is None
        random_start : bool, optional
            whether or not to initialize the perturbation with small isotropic
            Gaussian noise. Default is False
        guide_mode : int, optional
            Choose the guide_mode to use between 1 and 2. Default is 1
            - guide_mode == 1: find m nearest neighbors to input that all have
            the same class but not equal its original label.
            - guide_mode == 2: find the nearest neighbor that has a different
            class from the input and find its m - 1 neighbors

        Returns
        -------
        x_adv : torch.tensor
            adversarial examples found. If adversarial examples for some inputs
            are not found, return those inputs.
        """

        min_, max_ = x_orig.min(), x_orig.max()
        if max_linf is not None:
            min_ = torch.max(x_orig - max_linf, min_)
            max_ = torch.min(x_orig + max_linf, max_)
        batch_size = x_orig.size(0)
        x_adv = x_orig.clone()
        label = label.cpu().numpy()
        input_shape = x_orig.detach().cpu().numpy().shape
        device = dknn.device

        def to_attack_space(x):
            # map from [min_, max_] to [-1, +1]
            a = (min_ + max_) / 2
            b = (max_ - min_) / 2
            x = (x - a) / b

            # from [-1, +1] to approx. (-1, +1)
            x = x * 0.999999

            # from (-1, +1) to (-inf, +inf)
            return self.atanh(x)

        def to_model_space(x):
            """Transforms an input from the attack space
            to the model space. This transformation and
            the returned gradient are elementwise."""

            # from (-inf, +inf) to (-1, +1)
            x = torch.tanh(x)

            # map from (-1, +1) to (min_, max_)
            a = (min_ + max_) / 2
            b = (max_ - min_) / 2
            x = x * b + a

            return x

        # variables representing inputs in attack space will be prefixed with z
        z_orig = to_attack_space(x_orig)
        x_recon = to_model_space(z_orig)

        # declare tensors that keep track of constants and binary search
        const = torch.zeros((batch_size, ), device=device)
        const += initial_const
        lower_bound = torch.zeros_like(const)
        upper_bound = torch.zeros_like(const) + INFTY

        with torch.no_grad():

            # choose guide samples and get their representations
            if guide_mode == 1:
                x_guide = self.find_guide_samples(
                    dknn, x_orig, label, k=m, layer=guide_layer)
            elif guide_mode == 2:
                x_guide = self.find_guide_samples_v2(
                    dknn, x_orig, label, k=m, layer=guide_layer)
            else:
                raise ValueError("Invalid guide_mode (choose between 1 and 2)")

            guide_reps = {}
            for i in range(batch_size):
                guide_rep = dknn.get_activations(
                    x_guide[i], requires_grad=False)
                for layer in dknn.layers:
                    if i == 0:
                        # set a zero tensor before filling it
                        size = (batch_size, ) + \
                            guide_rep[layer].view(m, -1).size()
                        guide_reps[layer] = torch.zeros(size, device=device)
                    guide_reps[layer][i] = guide_rep[layer].view(
                        m, -1).detach()

        for binary_search_step in range(binary_search_steps):

            if not random_start:
                z_delta = torch.zeros_like(z_orig, requires_grad=True)
            else:
                rand = np.random.randn(*input_shape) * 1e-2
                z_delta = torch.tensor(
                    rand, dtype=torch.float32, requires_grad=True, device=device)
            loss_at_previous_check = torch.zeros(1, device=device) + INFTY

            # create a new optimizer
            optimizer = optim.Adam([z_delta], lr=learning_rate)
            # optimizer = optim.SGD([z_delta], lr=learning_rate)

            for iteration in range(max_iterations):
                optimizer.zero_grad()
                x = to_model_space(z_orig + z_delta)
                reps = dknn.get_activations(x, requires_grad=True)
                loss, l2dist = self.loss_function(
                    x, reps, guide_reps, dknn.layers, const, x_recon, device)
                loss.backward()
                optimizer.step()

                if iteration % (np.ceil(max_iterations / 10)) == 0:
                    print('    step: %d; loss: %.3f; l2dist: %.3f' %
                          (iteration, loss.cpu().detach().numpy(),
                           l2dist.mean().cpu().detach().numpy()))
                # DEBUG:
                # for i in range(5):
                #     print(z_delta.grad[i].view(-1).norm().item())

                if abort_early and iteration % (np.ceil(max_iterations / 10)) == 0:
                    # after each tenth of the iterations, check progress
                    if torch.gt(loss, .9999 * loss_at_previous_check):
                        break  # stop Adam if there has not been progress
                    loss_at_previous_check = loss

            # check how many attacks have succeeded
            with torch.no_grad():
                is_adv = self.check_adv(dknn, x, label)

            for i in range(batch_size):
                # set new upper and lower bounds
                if l2dist[i] > 0:
                    lower_bound[i] = const[i]
                else:
                    upper_bound[i] = const[i]
                    if is_adv[i]:
                        x_adv[i] = x[i]
                # set new const
                if upper_bound[i] == INFTY:
                    # exponential search if adv has not satisfied the
                    # constraint once
                    const[i] *= 10
                elif lower_bound[i] == 0:
                    const[i] /= 10
                else:
                    # binary search if adv has been found
                    const[i] = (lower_bound[i] + upper_bound[i]) / 2

            # check the current attack success rate
            with torch.no_grad():
                is_adv = self.check_adv(dknn, x_adv, label)
            print('binary step: %d; number of successful adv: %d/%d' %
                  (binary_search_step, is_adv.sum(), batch_size))

        return x_adv

    @classmethod
    def check_adv(cls, dknn, x, label):
        """Check if label of <x> predicted by <dknn> matches with <label>"""
        y_pred = dknn.classify(x).argmax(1)
        return torch.tensor((y_pred != label).astype(np.float32)).to(dknn.device)

    @classmethod
    def loss_function(cls, x, reps, guide_reps, layers, const, x_recon, device):
        """Returns the loss averaged over the batch (first dimension of x) and
        L-2 norm squared of the perturbation
        """

        batch_size = x.size(0)
        adv_loss = torch.zeros((batch_size, len(layers)), device=device)
        # find squared L-2 distance between original samples and their
        # adversarial examples at each layer
        for l, layer in enumerate(layers):
            rep = reps[layer].view(batch_size, 1, -1)
            adv_loss[:, l] = ((rep - guide_reps[layer])**2).sum((1, 2))
        # find L-inf norm squared of perturbation
        dist = torch.max(torch.zeros_like(x), torch.abs(x - x_recon) - 0.1)
        dist = (dist**2).view(batch_size, -1).mean(1)
        # total_loss is sum of squared perturbation norm and squared distance
        # of representations, multiplied by constant
        total_loss = const * dist + adv_loss.mean(1)

        return total_loss.mean(), dist.sqrt()

    @staticmethod
    def find_guide_samples(dknn, x, label, k=100, layer='relu1'):
        """Find k nearest neighbors to <x> that all have the same class but not
        equal to <label>
        """
        num_classes = dknn.num_classes
        nn = torch.zeros((k, ) + x.size()).transpose(0, 1)
        D, I = dknn.get_neighbors(
            x, k=dknn.x_train.size(0), layers=[layer])[0]

        for i, (d, ind) in enumerate(zip(D, I)):
            mean_dist = np.zeros((num_classes, ))
            for j in range(num_classes):
                mean_dist[j] = np.mean(
                    d[np.where(dknn.y_train[ind] == j)[0]][:k])
            mean_dist[label[i]] += INFTY
            nearest_label = mean_dist.argmin()
            nn_ind = np.where(dknn.y_train[ind] == nearest_label)[0][:k]
            nn[i] = dknn.x_train[ind[nn_ind]]

        return nn

    @classmethod
    def find_guide_samples_v2(cls, dknn, x, label, k=100, layer='relu1'):
        """Find the nearest neighbor to <x> that has a different label from
        <label>. Then find other <k> - 1 training samples that are closest to
        the neighbor and has the same class
        """
        # find nearest sample with different class
        nn = dknn.find_nn_diff_class(x, label)
        # now find k neighbors that has the same class as x_nn
        x_nn = cls.find_nn_same_class(dknn, nn, k=k, layer=layer)
        return x_nn

    @staticmethod
    def find_nn_same_class(dknn, ind_x, k=100, layer='relu1'):
        """Find <k> training samples with the same class as and closest to the
        training sample with index <ind_x> in representation space at <layer>
        """

        batch_size = ind_x.shape[0]
        label = dknn.y_train[ind_x]
        x_nn = torch.zeros((batch_size, k) + dknn.x_train[0].size())
        _, I = dknn.get_neighbors(
            dknn.x_train[ind_x], k=dknn.x_train.size(0), layers=[layer])[0]

        for i, ind in enumerate(I):
            nn_ind = np.where(dknn.y_train[ind] == label[i])[0][:k]
            x_nn[i] = dknn.x_train[ind[nn_ind]]

        return x_nn

    @staticmethod
    def atanh(x):
        return 0.5 * torch.log((1 + x) / (1 - x))

    @staticmethod
    def sigmoid(x, a=1):
        return 1 / (1 + torch.exp(-a * x))
