########################################################################################
# 
# Forge
# Copyright (C) 2018  Adam R. Kosiorek, Oxford Robotics Institute and
#     Department of Statistics, University of Oxford
#
# email:   adamk@robots.ox.ac.uk
# webpage: http://akosiorek.github.io/
# github: https://github.com/akosiorek/forge/
# 
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
# 
########################################################################################

"""Tools used by the experiment script."""
from __future__ import print_function
import imp
import importlib
import os
import os.path as osp
import sys
import re
import shutil
import simplejson as json
import subprocess

import numpy as np

from forge import flags as _flags

FLAG_FILE = 'flags.json'


def json_store(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, sort_keys=True, indent=4)


def json_load(path):
    with open(path, 'r') as f:
        return json.load(f)


def init_checkpoint(checkpoint_dir, data_config, model_config, resume):
    """Initializes model checkpoint.

    This function ensures that the `checkpoint_dir` exists and assigns a folder
    under `checkpoint_dir` to this particular job. Job folders' names are integer values
    starting at 0.

    If `resume` is True, the folder for this job is set to the highest
    already existing number and the checkpoint with the highest global_step value from that
    folder is chosen to resume the model from. Additionally, config flags are loaded from this job folder.
    If `resume` is False, a new job folder is created.

    `data_config` and `model_config` are used to load and parse config flags that might be
    defined in these config files. If

    :param checkpoint_dir: path to a checkpoint dir.
    :param data_config: path to a data config.
    :param model_config: path to a model config.
    :param resume: boolean; tries to resume the model from a checkpoint if True.
    :return: path to the experiment folder, parsed config flags, path to model checkpoint that the model
        should be resumed from or None.
    """

    # # Make sure these are absolute paths as otherwise model loading becomes tricky.
    # data_config, model_config = (osp.abspath(i) for i in (data_config, model_config))

    # check if the experiment folder exists and create if not
    checkpoint_dir_exists = os.path.exists(checkpoint_dir)
    if not checkpoint_dir_exists:
        if resume:
            raise ValueError("Can't resume when the checkpoint dir '{}' doesn't exist.".format(checkpoint_dir))
        else:
            os.makedirs(checkpoint_dir)

    elif not os.path.isdir(checkpoint_dir):
        raise ValueError("Checkpoint dir '{}' is not a directory.".format(checkpoint_dir))

    # find all job folders
    experiment_folders = [f for f in os.listdir(checkpoint_dir)
                          if not f.startswith('_') and not f.startswith('.')]

    if experiment_folders:
        experiment_folder = int(sorted(experiment_folders, key=lambda x: int(x))[-1])
        if not resume:
            experiment_folder += 1
    else:
        if resume:
            raise ValueError("Can't resume since no experiments were run before in checkpoint"
                             " dir '{}'.".format(checkpoint_dir))
        else:
            experiment_folder = 1

    experiment_folder = os.path.join(checkpoint_dir, str(experiment_folder))
    if not resume:
        os.mkdir(experiment_folder)

    flag_path = os.path.join(experiment_folder, FLAG_FILE)
    resume_checkpoint = None

    # parse flags from model/data config files
    # TODO(akosiorek): is there a way to remove `_load_flags` call here?
    _load_flags(model_config, data_config)
    flags = parse_flags()
    assert_all_flags_parsed()

    # restore flags and find the latest model checkpoint
    # TODO(akosiorek): this should use `load_from_checkpoint` function
    if resume:
        restored_flags = json_load(flag_path)
        flags.update(restored_flags)
        _restore_flags(flags)
        model_files = find_model_files(experiment_folder)
        if model_files:
            resume_checkpoint = model_files[max(model_files.keys())]

    else:
        # store flags
        try:
            flags['git_commit'] = get_git_revision_hash()
        except subprocess.CalledProcessError:
            # not in repo
            pass

        # save config flags
        json_store(flag_path, flags)

        # copy model/data config to run folder
        for src in (model_config, data_config):
            file_name = os.path.basename(src)
            dst = os.path.join(experiment_folder, file_name)
            shutil.copy(src, dst)

    return experiment_folder, resume_checkpoint


def extract_itr_from_modelfile(model_path):
    return int(model_path.split('-')[-1].split('.')[0])


def find_model_files(model_dir):
    """Finds model checkpoints"""
    pattern = re.compile(r'.ckpt-[0-9]+$')
    model_files = [f.replace('.index', '') for f in os.listdir(model_dir)]
    model_files = [f for f in model_files if pattern.search(f)]
    model_files = {extract_itr_from_modelfile(f): os.path.join(model_dir, f) for f in model_files}
    return model_files


def load(conf_path, *args, **kwargs):
    """Loads a config."""

    module, name = _import_module(conf_path)
    try:
        load_func = module.load
    except AttributeError:
        raise ValueError("The config file should specify 'load' function but no such function was "
                           "found in {}".format(module.__file__))

    print("Loading '{}' from {}".format(module.__name__, module.__file__))
    parse_flags()
    return load_func(*args, **kwargs)


def _import_module(module_path_or_name):
    """Dynamically imports a module from a filepath or a module name."""
    module, name = None, None

    if module_path_or_name.endswith('.py'):

        if not os.path.exists(module_path_or_name):
            raise RuntimeError('File {} does not exist.'.format(module_path_or_name))

        file_name = module_path_or_name
        module_path_or_name = os.path.basename(os.path.splitext(module_path_or_name)[0])
        if module_path_or_name in sys.modules:
            module = sys.modules[module_path_or_name]
        else:
            module = imp.load_source(module_path_or_name, file_name)
    else:
        module = importlib.import_module(module_path_or_name)

    if module:
        name = module_path_or_name.split('.')[-1].split('/')[-1]

    return module, name


def _load_flags(*config_paths):
    """Aggregates gflags from `config_path` into global flags.

    :param config_paths: list of config paths
    """
    for config_path in config_paths:
        print('loading flags from', config_path)
        _import_module(config_path)


def parse_flags():
    """Ensures that all flags are parsed."""
    f = _flags.FLAGS
    args = sys.argv[1:]

    old_flags = f.__dict__['__flags'].copy()
    # Parse the known flags from that list, or from the command
    # line otherwise.
    flags_passthrough = f._parse_flags(args=args)  # pylint: disable=protected-access
    sys.argv[1:] = flags_passthrough
    f.__dict__['__flags'].update(old_flags)

    return f.__flags  # pylint: disable=protected-access


def _restore_flags(flags):
    """Restores flags."""
    _flags.FLAGS.__dict__['__flags'] = flags
    _flags.FLAGS.__dict__['__parsed'] = True


def print_flags():
    """Pretty-prints config flags."""
    flags = _flags.FLAGS.__flags

    print('Flags:')
    keys = sorted(flags.keys())
    print('=' * 60)
    for k in keys:
        print('\t{}: {}'.format(k, flags[k]))
    print('=' * 60)
    print


def set_flags(**flag_dict):
    """Sets command-file flags."""
    for k, v in flag_dict.iteritems():
       sys.argv.append('--{}={}'.format(k, v))


def assert_all_flags_parsed():
    not_parsed = [a for a in sys.argv[1:] if a.startswith('--')]
    if not_parsed:
        raise RuntimeError('Failed to parse following flags: {}'.format(not_parsed))


def get_git_revision_hash():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip()


def set_flags_if_notebook(**flags_to_set):
    """Sets flags ONLY IF executed in a jupyter notebook runtime.

    This function is useful when loading a model or a dataset from a config inside a notebook,
    or when some python script is called from a notebook.
    """
    if is_notebook() and flags_to_set:
        print('Setting the following flags:')
        keys = sorted(flags_to_set.keys())
        for k in keys[:-1]:
            print(' --{}={}\\'.format(k, flags_to_set[k]))

        k = keys[-1]
        print(' --{}={}'.format(k, flags_to_set[k]))

        set_flags(**flags_to_set)


def is_notebook():
    """Determines whether the python is run under jupyter notebook."""
    notebook = False
    try:
        interpreter = get_ipython().__class__.__name__
        if interpreter == 'ZMQInteractiveShell':
            notebook = True
        elif interpreter != 'TerminalInteractiveShell':
            raise ValueError('Unknown interpreter name: {}'.format(interpreter))

    except NameError:
        # get_ipython is undefined => no notebook
        pass
    return notebook


def format_integer(number, group_size=3):
    assert group_size > 0

    number = str(number)
    parts = []

    while number:
        number, part = number[:-group_size], number[-group_size:]
        parts.append(part)

    number = ' '.join(reversed(parts))
    return number


def set_gpu(gpu_num):
    os.environ['CUDA_VISIBLE_DEVICES'] = gpu_num