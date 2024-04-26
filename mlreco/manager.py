"""Main driver class for SPICE.

Takes care of everything in one centralized location:
    - Data loading
    - ML model initialization/forward path
    - Representation building
    - Post-processing
    - Writing
"""

import os
import glob
from collections import defaultdict
from warnings import warn
from datetime import datetime
from inspect import signature

import psutil
import numpy as np
import torch

from torch.nn.parallel import DistributedDataParallel as DDP

from .data import TensorBatch, IndexBatch, EdgeIndexBatch

from .io.factories import loader_factory, writer_factory
from .io.writers import CSVWriter

from .models import model_factory
from .models.experimental.bayes.calibration import (
        calibrator_factory, calibrator_loss_factory)

from .utils.stopwatch import StopwatchManager
from .utils.unwrap import Unwrapper
from .utils.train import optim_factory, lr_sched_factory
from .utils.logger import logger
from .utils.torch_local import cycle # TODO: get rid of this


class SPICEManager(TrainVal):
    """Groups all relevant functions to drive SPICE.""" 

    def __init__(self, io, trainval, model, rank=0):
        """Initializes the class attributes.

        Parameters
        ----------
        io : dict
           Input/output configuration dictionary
        trainval : dict
           Main configuration dictionary
        model : dict
           Model configuration dictionary
        rank : int, default 0
           Rank of the GPU in the multi-GPU training process
        """
        # Store the rank of the training process for multi-GPU training
        self.rank = rank
        self.main_process = rank == 0

        # Store the configuration as is
        self.cfg = {'io': io, 'model': model, 'trainval': trainval}

        # Parse the main configuration
        self.process_main(**trainval)

        # Parse the model configuration
        self.process_model(**model)

        # Parse the I/O configuration
        self.process_io(**io)

        # Initialize the timers
        self.watch = StopwatchManager()
        self.watch.initialize(['iteration', 'io', 'forward', 'backward',
                               'unwrap', 'save', 'write'])

        # Initialize the object
        self.initialize()

    def process_main(self, gpus=None, distributed=False, model_path=None,
                     weight_prefix='weights/snapshot', log_dir='logs',
                     iterations=None, epochs=None, report_step=1,
                     checkpoint_step=-1, train=True, seed=-1, optimizer=None,
                     lr_scheduler=None, time_dependant_loss=False,
                     restore_optimizer=False, to_numpy=True, unwrap=False,
                     detect_anomaly=False, find_unused_parameters=False):
        """Process the trainval configuration.

        Parameters
        ----------
        gpus : list, optional
            List of GPU IDs to run the model on
        distributed : bool, default False
            Whether to distribute the training/inference process to > 1 GPU
        model_path : str, optional
            Path to the model weights to restore
        weight_prefix : str, default 'weights/snapshot'
            Path prefix to the location where to store the weights
        log_dir : str, default 'logs'
            Path to the directory in which to store the training/inference log
        iterations : int, optional
            Number of iterations to run through (-1: whole dataset once)
        epochs : float, optional
            Number of epochs to run through (can be fractional)
        report_step : int, default 1
            Number of iterations before the logging is called (1: every step)
        checkpoint_step : int, default -1
            Number of iterations before recording the model weights (-1: never)
        train : bool, default True
            Whether model weights must be updated or not (train vs inference)
        seed : int, default -1
            Random seed for the training process
        optimizer : dict, optional
            Configuration of the optimizer (only needed to train)
        restore_optimizer : bool, default False
            Whether to load the  opimizer state from the torch checkpoint
        lr_scheduler : dict, optional
            Configuration of the learning rate scheduler
        time_dependant_loss : bool, default False
            Handles time-dependant loss, such as KL divergence annealing
        to_numpy : bool, default True
            Cast the input/output tensors to np.ndarrays to be used downstream
        unwrap : bool, default False
            Whether to unwrap the forward output, one per data entry
        detect_anomaly : bool, default False
            Whether to attempt to detect a torch anomaly
        find_unused_parameters : bool, default False
            Attempts to detect unused model parameters in the forward pass
        """
        # Store the relevant information
        self.gpus                   = gpus
        self.distributed            = distributed
        self.model_path             = model_path
        self.weight_prefix          = weight_prefix
        self.log_dir                = log_dir
        self.iterations             = iterations
        self.epochs                 = epochs
        self.report_step            = report_step
        self.checkpoint_step        = checkpoint_step
        self.train                  = train
        self.time_dependant         = time_dependant_loss
        self.to_numpy               = to_numpy
        self.unwrap                 = unwrap
        self.find_unused_parameters = find_unused_parameters

        # If the seed is provided, set it for the master process only
        if self.main_process:
            np.random.seed(seed)
            torch.manual_seed(seed)

        # If anomaly detection is requested, set it for the master process
        if self.main_process and detect_anomaly:
            torch.autograd.set_detect_anomaly(True, check_nan=True)

        # If there is more than one GPU available, must distribute
        self.world_size = max(1, len(self.gpus))
        if self.world_size > 1:
            self.distributed = True

        # If unwrapping is requested, must cast to numpy
        if self.unwrap:
            self.to_numpy = True

        # Should not specify iterations and epochs at the same time
        assert (self.iterations is not None) ^ (self.epochs is not None), (
                "Must either specify `iterations` or `epochs` in `trainval`")

        # Store the optimizer configuration
        if self.train:
            assert optimizer is not None, (
                    "Must provide an optimizer configuration block to train")
            self.optim_cfg = optimizer
            self.restore_optimizer = restore_optimizer

        # Store the learning-rate scheduler configuration
        if self.train:
            self.lr_sched_cfg = lr_scheduler

    def process_model(self, name, modules, network_input, loss_input=None,
                      keep_output=None, ignore_keys=None, calibration=None):
        """Process the model configuration.

        Parameters
        ----------
        name : str
            Name of the model as specified under mlreco.models.factories
        modules : dict
            Dictionary of modules that make up the model
        network_input : List[str]
            List of keys of parsed objects to input into the model forward
        loss_input : List[str], optional
            List of keys of parsed objects to input into the loss forward
        keep_output : List[str], optional
            List of keys to provide in the model forward output
        ignore_keys : List[str], optional
            List of keys to ommit in the model forward output
        calibration : dict, optional
            Model score calibration configuration
        """
        # Fetch the relevant model, store arguments
        self.model_name  = name
        self.model_cfg   = modules
        self.model_class, self.loss_class = model_factory(name)

        # Store the score calibration configuration
        self.calibration = calibration

        # Store the list of input keys to the forward/loss functions. These
        # should be specified as a dictionary mapping the name of the argument
        # in the forward/loss function to a data product name.
        self.input_dict = network_input
        self.loss_dict  = loss_input

        if not isinstance(network_input, dict):
            warn("Specify `network_input` as a dictionary, not a list.",
                 DeprecationWarning)
            fn   = self.model_class.forward
            keys = list(signature(fn).parameters.keys())[1:] # Skip `self`
            num_input = len(network_input)
            self.input_dict = {
                    keys[i]:network_input[i] for i in range(num_input)}

        if loss_input is not None and not isinstance(loss_input, dict):
            warn("Specify `loss_input` as a dictionary, not a list.",
                 DeprecationWarning)
            fn   = self.loss_class.forward
            keys = list(signature(fn).parameters.keys())[1:] # Skip `self`
            num_input = len(loss_input)
            self.loss_dict = {keys[i]:loss_input[i] for i in range(num_input)}

        # Parse the list of output to be stored
        assert not (keep_output and ignore_keys), (
                "Should not specify `keep_output` and `ignore_keys` together.")
        self.output_keys = keep_output
        self.ignore_keys = ignore_keys

    def process_io(self, collate_fn=None, writer=None, **io_cfg):
        """Initialize the dataloader.

        Parameters
        ----------
        collate_fn : dict, optional
            Dictionary of collate function and collate parameters, if any
        writer : dict, optional
            Writer configuration dictionary
        **io_cfg : dict
            Rest of the input/output configuration dictionary
        """
        # Initialize the dataloader
        self.loader = loader_factory(
                collate_fn=collate_fn, distributed=self.distributed,
                world_size=self.world_size, rank=self.rank, **io_cfg)
        self.loader_iter = iter(cycle(self.loader))

        # Infer the total number of epochs from iterations or vice-versa
        iter_per_epoch = len(self.loader) / self.world_size
        if self.iterations is not None:
            if self.iterations < 0:
                self.epochs = 1
            else:
                self.epochs = self.iterations / iter_per_epoch
        else:
            self.iterations = self.epochs * iter_per_epoch

        # Get the number of volumes the collate function splits the input into
        self.geo = None
        self.num_volumes = 1
        if (hasattr(self.loader, 'collate_fn') and
            hasattr(self.loader.collate_fn, 'geo')):
            self.geo = self.loader.collate_fn.geo
            self.num_volumes = self.geo.num_modules

        # Initialize the writer, if provided
        self.writer = None
        if writer is not None:
            if not self.unwrap:
                raise ValueError(
                        "Must set `unwrap` to True when writing to file")
            self.writer = writer_factory(writer)

    def initialize(self):
        """Initialize the necessary building blocks to train a model."""
        # Initialize model and loss function
        try:
            self.model = self.model_class(**self.model_cfg)
        except Exception as err:
            msg = f"Failed to instantiate {self.model_class}"
            raise type(err)(f"{err}\n{msg}")

        try:
            self.loss_fn = self.loss_class(**self.model_cfg)
        except Exception as err:
            msg = f"Failed to instantiate {self.loss_class}"
            raise type(err)(f"{err}\n{msg}")

        # Replace model with calibrated model on uncertainty calibration mode
        if self.calibration is not None:
            self.initialize_calibrator(**self.calibration)

        # If GPUs are available, move the model and loss function
        if len(self.gpus):
            self.model.to(self.rank)
            self.loss_fn.to(self.rank)

        # If multiple GPUs are used, wrap with DistributedDataParallel
        if self.distributed:
            self.model = DDP(
                    self.model, device_ids=[self.rank],
                    output_device=self.rank,
                    find_unused_parameters=self.find_unused_parameters)

        # Set the model in train or evaluation mode
        if self.train:
            self.model.train()
        else:
            self.model.eval()

        # Initiliaze the optimizer
        if self.train:
            self.optimizer = optim_factory(
                    self.optim_cfg, self.model.parameters())

        # Initialize the learning rate scheduler
        if self.train:
            self.lr_scheduler = None
            if self.lr_sched_cfg is not None:
                self.lr_scheduler = lr_sched_factory(
                        self.lr_sched_cfg, self.optimizer)

        # Module-by-module parameter freezing
        self.freeze_weights()

        # Module-by-module parameter loading
        self.load_weights(self.model_path)

        # Initialize the unwrapper
        if self.unwrap:
            self.unwrapper = Unwrapper(self.geo, remove_batch_col=True)

        # Create the output log/checkpoint directories
        self.make_directories()

    def freeze_weights(self):
        """Freeze the weights of certain model components.

        Breadth-first search for `freeze_weights` parameters in the model
        configuration. If `freeze_weights` is `True` under a module block,
        `requires_grad` is set to `False` for its parameters. The batch
        normalization and dropout layers are set to evaluation mode.
        """
        # Loop over all the module blocks in the model configuration
        module_items = list(self.model_cfg.items())
        while len(module_items) > 0:
            # Get the module name and its configuration block
            module, config = module_items.pop()

            # If the module is to be frozen, apply
            if config.get('freeze_weights', False):
                # Fetch the module name to be found in the state dictionary
                model_name = config.get('model_name', module)

                # Set BN and DO layers to evaluation mode
                getattr(self.model, module).eval()

                # Freeze all the weights of this module
                count = 0
                for name, param in self.model.named_parameters():
                    if module in name:
                        key = name.replace(f'.{module}.', f'.{model_name}.')
                        if key in self.model.state_dict().keys():
                            param.requires_grad = False
                            count += 1

                # Throw if no weights were found to freeze
                assert count, (
                        f"Could not find any weights to freeze for {module}")

                logger.info("Froze %d weights in module %s", count, module)

            # Keep the BFS going by adding the nested blocks
            for key in config:
                if isinstance(config[key], dict):
                    module_items.append((key, config[key]))

    def load_weights(self, full_model_path):
        """Load the weights of certain model components.

        Breadth-first search for `model_path` parameters in the model
        configuration. If 'model_path' is found under a module block,
        the weights are loaded for its parameters.

        If a `model_path` is not found for a given module, load the overall
        weights from `model_path` under `trainval` for that module instead.

        Parameters
        ----------
        full_model_path : str
            Path to the weights for the full model
        """
        # If a general model path is provided, add it to the loading list first
        model_paths = []
        if full_model_path:
            model_paths = [(self.model_name, full_model_path, '')]

        # Find the list of sub-module weights to subsequently load
        module_items = list(self.model_cfg.items())
        while len(module_items) > 0:
            module, config = module_items.pop()
            if config.get('model_path', '') != '':
                model_name = config.get('model_name', module)
                model_paths.append((module, config['model_path'], model_name))
            for key in config:
                if isinstance(config[key], dict):
                    module_items.append((key, config[key]))

        # If no pre-trained weights are requested, nothing to do here
        self.start_iteration = 0
        if not model_paths:
            return

        # Loop over provided model paths
        for module, model_path, model_name in model_paths:
            # Check that the requested weight file can be found. If the path
            # points at > 1 file, skip for now (loaded in a loop later)
            if not os.path.isfile(model_path):
                if not self.train and glob.glob(model_path):
                    continue

                raise ValueError("Weight file not found for module "
                                f"{module}: {model_path}")

            # Load weight file into existing model
            logger.info("Restoring weights for module %s "
                        "from %s...", module, model_path)
            with open(model_path, 'rb') as f:
                # Read checkpoint. If loading weights to a non-distributed
                # model, remove leading keyword `module` from weight names.
                checkpoint = torch.load(f, map_location='cpu')
                state_dict = checkpoint['state_dict']
                if not self.distributed:
                    state_dict = {k.replace('module.', ''):v \
                            for k, v in state_dict.items()}

                # Check that all the needed weights are provided
                missing_keys = []
                if module == self.model_name:
                    for name in self.model.state_dict():
                        if not name in state_dict.keys():
                            missing_keys.append((name, name))
                else:
                    # Update the key names according to the name used to store
                    state_dict = {}
                    for name in self.model.state_dict():
                        if module in name:
                            key = name.replace(
                                    f'.{module}.', f'.{model_name}.')
                            if key in checkpoint['state_dict'].keys():
                                state_dict[name] = checkpoint['state_dict'][key]
                            else:
                                missing_keys.append((name, key))

                # If some necessary keys were not found, throw
                if missing_keys:
                    logger.critical(
                            "These necessary parameters could not be found:")
                    for name, key in missing_keys:
                        logger.critical(
                                "Parameter %s is missing for %s.", key, name)
                    raise ValueError("To be loaded, a set of weights "
                                     "must provide all necessary parameters.")

                # Load checkpoint. Check that all weights are used
                bad_keys = self.model.load_state_dict(state_dict, strict=False)
                if len(bad_keys.unexpected_keys) > 0:
                    logger.warning(
                            "This weight file contains parameters that could "
                            "not be loaded, indicating that the weight file "
                            "contains more than needed. This might be ok.")
                    logger.warning(
                            'Unexpected keys: %s', bad_keys.unexpected_keys)

                # Load the optimizer state from the main weight file only
                if (self.train and
                    module == self.model_name and
                    self.restore_optimizer):
                    self.optimizer.load_state_dict(checkpoint['optimizer'])

                # Get the latest iteration from the main weight file only
                if module == self.model_name:
                    self.start_iteration = checkpoint['global_step'] + 1

            logger.info('Done.')

    def initialize_calibrator(self, calibrator, calibrator_loss):
        """Switch model to calibration mode.

        Allows to calibrate logits to respond linearly to probability,
        for instance.

        Parameters
        ----------
        calibrator : dict
            Calibrator configuration dictionary
        calibrator_loss : dict
            Calibrator loss configuration dictionary
        """
        # Switch to calibration mode
        self.model = calibrator_factory(model=self.model, **calibrator)
        self.loss_fn = calibrator_loss_factory(**calibrator_loss)

    def make_directories(self):
        """Make directories as to where to store the logs and weights."""
        # Create weight save directory if it does not exist
        save_dir = os.path.dirname(self.weight_prefix)
        if save_dir and not os.path.isdir(save_dir):
            os.makedirs(save_dir, exist_ok=True)

        # Create log save directory if it does not exist, initialize logger
        if self.log_dir and not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir, exist_ok=True)
        prefix   = 'train' if self.train else 'inference'
        suffix   = '' if not self.distributed else f'_proc{self.rank}'
        logname  = f'{self.log_dir}/'
        logname += f'{prefix}{suffix}_log-{self.start_iteration:07d}.csv'

        self.csv_logger = CSVWriter(logname)

    def run(self):
        """Run the training or inference loop on the amount of iterations or
        epochs requested.
        """
        # Loop until the requested amount of iterations/epochs is reached
        iter_per_epoch = len(self.loader) / self.world_size
        iteration = self.start_iteration if self.train else 0
        num_epochs = int(np.ceil(self.epochs - iteration / iter_per_epoch))
        for e in range(num_epochs):
            if self.distributed:
                self.loader.sampler.set_epoch(e)

            self.loader_iter = iter(self.loader)
            n_iterations = min(
                    self.iterations - e*len(self.loader), len(self.loader))
            for _ in range(n_iterations):
                # Update the epoch counter, start the iteration timer
                epoch = iteration / iter_per_epoch
                tstamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.watch.start('iteration')

                # Run the forward/backward functions (includes data loading)
                data, result = self.train_step(iteration)

                # Save the model weights if training
                self.watch.start('save')
                save_step = ((iteration + 1)  % self.checkpoint_step) == 0
                if self.train and save_step and self.main_process:
                    self.save_state(iteration)
                self.watch.stop('save')

                # If requested, store the output of the forward function
                self.watch.start('write')
                if self.writer is not None:
                    self.write(data, result)
                self.watch.stop('write')

                # Stop the iteration timer
                self.watch.stop('iteration')

                # Log the information if needed
                self.log(result, tstamp, iteration, epoch, data['index'][0])

                # Increment iteration counter
                iteration += 1

    def train_step(self, iteration=None):
        """Run one step of the training process.

        Parameters
        ----------
        iteration : int, optional
            Iteration step index

        Returns
        -------
        data : dict
            Dictionary of input data product keys which each map to an input
        result : dict
            Dictionary of forward output produt keys which each map an output
        """
        # Run the model forward
        data, result = self.forward(iteration)

        # Run backward once for the previous forward (if training)
        self.watch.start('backward')
        if self.train:
            self.backward()
        self.watch.stop('backward')

        return data, result

    def forward(self, iteration=None):
        """Run the forward function once over the batch.

        Parameters
        ----------
        iteration : int, optional
            Iteration step index

        Returns
        -------
        data : dict
            Dictionary of input data product keys which each map to an input
        result : dict
            Dictionary of forward output produt keys which each map an output
        """
        # Get the batched data
        self.watch.start('io')
        data = next(self.loader_iter)
        input_dict, loss_dict = self.get_data_minibatch(data)
        self.watch.stop('io')

        # Run forward
        self.watch.start('forward')
        result = self.step(input_dict, loss_dict, iteration=iteration)
        self.watch.stop('forward')

        # Unwrap output, if requested
        self.watch.start('unwrap')
        if self.unwrap:
            data, result = self.unwrapper(data, result)
        self.watch.stop('unwrap')

        return data, result

    def get_data_minibatch(self, data):
        """Fetches the necessary data products to form the input to the forward
        function and the input to the loss function.

        Parameters
        ----------
        data : dict
            Dictionary of input data product keys which each map to its
            associated data product

        Returns
        -------
        input_dict : dict
            Input to the forward pass of the model
        loss_dict : dict
            Labels to be used in the loss computation
        """
        # Fetch the requested data products
        device = self.rank if self.gpus else None
        input_dict, loss_dict = {}, {}
        with torch.set_grad_enabled(self.train):
            # Load the data products for the model forward
            input_dict = {}
            for param, name in self.input_dict.items():
                assert name in data, (
                        f"Must provide {name} in the dataloader schema to "
                         "input into the model forward")

                value = data[name]
                if isinstance(value, TensorBatch):
                    value = data[name].to_tensor(torch.float, device)
                input_dict[param] = value

            # Load the data products for the loss function
            loss_dict = {}
            if self.loss_dict is not None:
                for param, name in self.loss_dict.items():
                    assert name in data, (
                            f"Must provide {name} in the dataloader schema to "
                             "input into the loss function")

                    value = data[name]
                    if isinstance(value, TensorBatch):
                        value = data[name].to_tensor(torch.float, device)
                    loss_dict[param] = value

        return input_dict, loss_dict

    def step(self, input_dict, loss_dict, iteration=None):
        """Step one minibatch of data through the network.

        Load one minibatch of data. pass it through the network forward
        function and the loss computation. Store the output.

        Parameters
        ----------
        input_dict : dict
            Input dictionary to the forward function
        loss_dict : dict
            Input dictionary to the loss function
        """
        # If in train mode, record the gradients for backward step
        with torch.set_grad_enabled(self.train):

            # Apply the model forward
            result = self.model(**input_dict)

            # Compute the loss if one is specified, append results
            self.loss = 0.
            if self.loss_dict:
                if not self.time_dependant:
                    result.update(self.loss_fn(**loss_dict, **result))
                else:
                    result.update(self.loss_fn(
                        iteration=iteration, **loss_dict, **result))

                if self.train:
                    self.loss = result['loss']

            # Filter and cast the output to numpy, if requested
            for key, value in result.items():
                # Skip keys that are not to be output
                if ((self.output_keys and key not in self.output_keys) or
                    (self.ignore_keys and key in self.ignore_keys)):
                    result.pop(key)

                # Convert to numpy, if requested
                if self.to_numpy:
                    if np.isscalar(value):
                        # Scalar
                        result[key] = value
                    elif (isinstance(value, torch.Tensor) and
                          value.numel() == 1):
                        # Scalar tensor
                        result[key] = value.item()
                    elif isinstance(
                            value, (TensorBatch, IndexBatch, EdgeIndexBatch)):
                        # Batch of data
                        result[key] = value.to_numpy()
                    elif (isinstance(value, list) and
                          len(value) and
                          isinstance(value[0], TensorBatch)):
                        # List of tensor batches
                        result[key] = [v.to_numpy() for v in value]
                    else:
                        raise ValueError(f"Cannot cast output {key} to numpy")

            return result

    def backward(self):
        """Run the backward step on the model."""
        # Reset the gradient accumulation
        self.optimizer.zero_grad()

        # Run the model backward
        self.loss.backward()

        # Step the optimizer
        self.optimizer.step()

        # Step the learning rate scheduler
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()

        # If the model has a buffer that needs to be updated, do it after
        # trainable parameter updates.
        model = self.model if not self.distributed else self.model.module
        if hasattr(model, 'update_buffers'):
            logger.info('Updating buffers')
            model.update_buffers()

    def save_state(self, iteration):
        """Save the model state.

        Save three things from the model:
        - global_step (iteration)
        - state_dict (model parameter values)
        - optimizer (optimizer parameter values)

        Parameters
        ----------
        iteration : int
            Iteration step index
        """
        # Make sure that the weight prefix is valid
        assert self.weight_prefix, (
                "Must provide a weight prefix to store them")

        filename = f'{self.weight_prefix}-{iteration:d}.ckpt'
        torch.save({
            'global_step': iteration,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict()
        }, filename)

    def write(self, data, result):
        """Write requested input/output to file.

        Parameters
        ----------
        data : dict
            Dictionary of input data product keys which each map to an input
        result : dict
            Dictionary of forward output produt keys which each map an output
        """
        # If the inference was distributed, gather the outptus
        if not self.distributed:
            self.writer.append(data, result, self.cfg)
        else:
            # Fetch the data from the distributed processes for the
            # required keys, build an aggregated dictionary
            data_keys, result_keys = self.writer.get_stored_keys(data, result)
            data_dict, result_dict = defaultdict(list), defaultdict(list)
            for k in data_keys:
                data_v = [None for _ in range(self.world_size)]
                torch.distributed.gather_object(data[k],
                        data_v if self.main_process else None, dst = 0)
                if np.isscalar(data_v[0]):
                    data_dict[k] = np.mean(data_v) \
                            if 'count' not in k else np.sum(data_v)
                elif isinstance(data_v[0], np.ndarray):
                    data_dict[k] = np.concatenate(data_v)
                elif isinstance(data_v[0], list):
                    for d in data_v:
                        data_dict[k].extend(d)

            for k in result_keys:
                result_v = [None for _ in range(self.world_size)]
                torch.distributed.gather_object(result[k],
                        result_v if self.main_process else None, dst = 0)
                if np.isscalar(result_v[0]):
                    result_dict[k] = np.mean(result_v) \
                            if 'count' not in k else np.sum(result_v)
                elif isinstance(result_v[0], np.ndarray):
                    result_dict[k] = np.concatenate(result_v)
                elif isinstance(result_v[0], list):
                    for r in result_v:
                        result_dict[k].extend(r)

            data_dict, result_dict = dict(data_dict), dict(result_dict)

            # Write only once (main process)
            self.writer.append(data_dict, result_dict, self.cfg)

    def log(self, result, tstamp, iteration, epoch, first_id):
        """Log relevant information to CSV files and stdout.

        Parameters
        ----------
        result : dict
            Output of the loss computation, which contains
            accuracy/loss metrics.
        iteration : int
            Iteration counter
        epoch : float
            Progress in the training process in number of epochs
        first_id : int
            ID of the first dataset entry in the batch
        tstamp : str
            Time when this iteration was run
        """
        # Fetch the basics
        log_dict = {
            'iter': iteration,
            'epoch': epoch,
            'first_id': first_id
        }

        # Fetch the memory usage (in GB)
        log_dict['cpu_mem'] = psutil.virtual_memory().used/1.e9
        log_dict['cpu_mem_perc'] = psutil.virtual_memory().percent
        log_dict['gpu_mem'], log_dict['gpu_mem_perc'] = 0., 0.
        if torch.cuda.is_available():
            gpu_total = torch.cuda.mem_get_info()[-1] / 1.e9
            log_dict['gpu_mem'] = torch.cuda.max_memory_allocated() / 1.e9
            log_dict['gpu_mem_perc'] = 100 * log_dict['gpu_mem'] / gpu_total

        # Fetch the times
        suff = '_time'
        for key, watch in self.watch.items():
            time, time_sum = watch.time, watch.time_sum
            log_dict[f'{key}{suff}'] = time.wall
            log_dict[f'{key}{suff}_cpu'] = time.cpu
            log_dict[f'{key}{suff}_sum'] = time_sum.wall
            log_dict[f'{key}{suff}_sum_cpu'] = time_sum.cpu

        # Fetch all the scalar outputs and append them to a dictionary
        for key in result:
            if np.isscalar(result[key]):
                log_dict[key] = result[key]

        # Record
        self.csv_logger.append(log_dict)

        # If requested, print out basics of the training/inference process.
        report_step = ((iteration + 1) % self.report_step) == 0
        if report_step:
            # Dump general information
            proc   = 'Train' if self.train else 'Inference'
            device = 'GPU' if self.gpus else 'CPU'
            keys   = [f'{proc} time', f'{device} memory', 'Loss', 'Accuracy']
            widths = [20, 20, 9, 9]
            if self.distributed:
                keys = ['Rank'] + keys
                widths = [5] + widths
            if self.main_process:
                header = '  | ' + '| '.join(
                        [f'{keys[i]:<{widths[i]}}' for i in range(len(keys))])
                separator = '  |' + '+'.join(['-'*(w+1) for w in widths])
                msg  = f"Iter. {iteration} (epoch {epoch:.3f}) @ {tstamp}\n"
                msg += header + '|\n'
                msg += separator + '|'
                print(msg, flush=True)
            if self.distributed:
                torch.distributed.barrier()

            # Dump information pertaining to a specific process
            t_iter = self.watch.time('iteration').wall
            t_net  = self.watch.time('forward').wall \
                    + self.watch.time('backward').wall

            if self.gpus:
                mem, mem_perc = log_dict['gpu_mem'], log_dict['gpu_mem_perc']
            else:
                mem, mem_perc = log_dict['cpu_mem'], log_dict['cpu_mem_perc']

            acc  = np.mean(result.get('accuracy', -1.))
            loss = np.mean(result.get('loss',     -1.))

            values = [f'{t_net:0.2f} s ({100*t_net/t_iter:0.2f} %)',
                      f'{mem:0.2f} GB ({mem_perc:0.2f} %)',
                      f'{loss:0.3f}', f'{acc:0.3f}']
            if self.distributed:
                values = [f'{self.rank}'] + values

            msg = '  | ' + '| '.join(
                    [f'{values[i]:<{widths[i]}}' for i in range(len(keys))])
            msg += '|'
            print(msg, flush=True)

            # Start new line once only
            if self.distributed:
                torch.distributed.barrier()
            if self.main_process:
                print('', flush=True)