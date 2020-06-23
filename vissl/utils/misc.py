#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import logging
import os
import random

import numpy as np
import torch
import torch.multiprocessing as mp
from scipy.sparse import csr_matrix


def is_apex_available():
    try:
        import apex  # NOQA

        apex_available = True
    except ImportError:
        apex_available = False
    return apex_available


def find_free_tcp_port():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Binding to port 0 will cause the OS to find an available port for us
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    # NOTE: there is still a chance the port could be taken by other processes.
    return port


def get_dist_run_id(cfg, num_nodes):
    """
    for 1node: use init_method=tcp and run_id=auto
    for multi-node, use init_method=tcp and specify run_id={master_node}:{port}
    """
    init_method = cfg.DISTRIBUTED.INIT_METHOD
    run_id = cfg.DISTRIBUTED.RUN_ID
    if init_method == "tcp" and cfg.DISTRIBUTED.RUN_ID == "auto":
        assert (
            num_nodes == 1
        ), "cfg.DISTRIBUTED.RUN_ID=auto is allowed for 1 machine only."
        port = find_free_tcp_port()
        run_id = f"127.0.0.1:{port}"
    elif init_method == "file" and num_nodes > 1:
        logging.warning(
            "file is not recommended to use for distributed training on > 1 node"
        )
    elif init_method == "tcp" and cfg.DISTRIBUTED.NUM_NODES > 1:
        assert cfg.DISTRIBUTED.RUN_ID, "please specify RUN_ID for tcp"
    elif init_method == "env":
        assert num_nodes == 1, "can not use 'env' init method for multi-node. Use tcp"
    return run_id


def setup_multiprocessing_method(method_name):
    try:
        mp.set_start_method(method_name, force=True)
        logging.info("Set start method of multiprocessing to {}".format(method_name))
    except RuntimeError:
        pass


def set_seeds(cfg, node_id=0):
    node_seed = cfg.SEED_VALUE
    if cfg.DISTRIBUTED.NUM_NODES > 1:
        node_seed = node_seed * 2 * node_id
    logging.info(f"MACHINE SEED: {node_seed}")
    random.seed(node_seed)
    np.random.seed(node_seed)
    torch.manual_seed(node_seed)
    if cfg["MACHINE"]["DEVICE"] == "gpu" and torch.cuda.is_available():
        torch.cuda.manual_seed_all(node_seed)


def get_indices_sparse(data):
    """
    Is faster than np.argwhere.
    """
    cols = np.arange(data.size)
    M = csr_matrix((cols, (data.ravel(), cols)), shape=(data.max() + 1, data.size))
    return [np.unravel_index(row.data, data.shape) for row in M]


def merge_features(output_dir, split, layer, cfg):
    logging.info(f"Merging features: {split} {layer}")
    output_feats, output_targets = {}, {}
    for local_rank in range(0, cfg.DISTRIBUTED.NUM_PROC_PER_NODE):
        for node_id in range(0, cfg.DISTRIBUTED.NUM_NODES):
            dist_rank = cfg.DISTRIBUTED.NUM_PROC_PER_NODE * node_id + local_rank
            feat_file = os.path.join(
                output_dir, f"rank{dist_rank}_{split}_{layer}_features.npy"
            )
            targets_file = os.path.join(
                output_dir, f"rank{dist_rank}_{split}_{layer}_targets.npy"
            )
            inds_file = os.path.join(
                output_dir, f"rank{dist_rank}_{split}_{layer}_inds.npy"
            )
            logging.info(f"Loading:\n{feat_file}\n{targets_file}\n{inds_file}")
            feats = np.load(feat_file)
            targets = np.load(targets_file)
            indices = np.load(inds_file)
            num_samples = feats.shape[0]
            for idx in range(num_samples):
                index = indices[idx]
                if not (index in output_feats):
                    output_feats[index] = feats[idx]
                    output_targets[index] = targets[idx]
    output = {}
    output_feats = dict(sorted(output_feats.items()))
    output_targets = dict(sorted(output_targets.items()))
    feats = np.array(list(output_feats.values()))
    N = feats.shape[0]
    output = {
        "features": feats.reshape(N, -1),
        "targets": np.array(list(output_targets.values())),
        "inds": np.array(list(output_feats.keys())),
    }
    logging.info(f"Features: {output['features'].shape}")
    logging.info(f"Targets: {output['targets'].shape}")
    logging.info(f"Indices: {output['inds'].shape}")
    return output