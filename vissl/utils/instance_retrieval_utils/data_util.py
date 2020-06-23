#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#


import logging
import math
import os
import pickle
import subprocess
from collections import OrderedDict

import numpy as np

# for INSTRE (it is in matlab)
import scipy.io
import torch
import torchvision.transforms.functional as TF
from PIL import Image, ImageFile
from torch.nn import functional as F
from torchvision import transforms
from vissl.utils.instance_retrieval_utils.evaluate import (
    compute_map,
    score_ap_from_ranks_1,
)


def is_revisited_dataset(dataset_name):
    if dataset_name in ["roxford5k", "rparis6k"]:
        return True
    return False


def is_instre_dataset(dataset_name):
    if dataset_name == "instre":
        return True
    return False


def is_whiten_dataset(dataset_name):
    if dataset_name == "whitening":
        return True
    return False


# pooling + whitening
# Credits: Matthijs Douze
def add_bias_channel(x, dim=1):
    bias_size = list(x.size())
    bias_size[dim] = 1
    one = x.new_ones(bias_size)
    return torch.cat((x, one), dim)


# Credits: Matthijs Douze
def flatten(x, keepdims=False):
    """
    Flattens B C H W input to B C*H*W output, optionally retains trailing dimensions.
    """
    y = x.view(x.size(0), -1)
    if keepdims:
        for _ in range(y.dim(), x.dim()):
            y = y.unsqueeze(-1)
    return y


# Credits: Matthijs Douze
def gem(x, p=3, eps=1e-6, clamp=True, add_bias=False, keepdims=False):
    if p == math.inf or p == "inf":
        x = F.max_pool2d(x, (x.size(-2), x.size(-1)))
    elif p == 1 and not (torch.is_tensor(p) and p.requires_grad):
        x = F.avg_pool2d(x, (x.size(-2), x.size(-1)))
    else:
        if clamp:
            x = x.clamp(min=eps)
        x = F.avg_pool2d(x.pow(p), (x.size(-2), x.size(-1))).pow(1.0 / p)
    if add_bias:
        x = add_bias_channel(x)
    if not keepdims:
        x = flatten(x)
    return x


# Credits: Matthijs Douze
def l2n(x, eps=1e-6, dim=1):
    x = x / (torch.norm(x, p=2, dim=dim, keepdim=True) + eps).expand_as(x)
    return x


# Credits: Matthijs Douze
class MultigrainResize(transforms.Resize):
    """
    Resize with a `largest=False` argument
    allowing to resize to a common largest side without cropping
    """

    def __init__(self, size, largest=False, **kwargs):
        super().__init__(size, **kwargs)
        self.largest = largest

    @staticmethod
    def target_size(w, h, size, largest=False):
        if (h < w) == largest:
            w, h = size, int(size * h / w)
        else:
            w, h = int(size * w / h), size
        size = (h, w)
        return size

    def __call__(self, img):
        size = self.size
        w, h = img.size
        target_size = self.target_size(w, h, size, self.largest)
        return TF.resize(img, target_size, self.interpolation)

    def __repr__(self):
        r = super().__repr__()
        return r[:-1] + f", largest={self.largest})"


# Credits: Matthijs Douze
class WhiteningTrainingImageDataset:
    """ A set of training images for whitening """

    def __init__(self, base_dir, image_list_file):
        with open(image_list_file) as fopen:
            self.image_list = fopen.readlines()
        self.root = base_dir
        self.N_images = len(self.image_list)
        logging.info(f"Loaded whitening data: {self.N_images}...")

    def get_num_images(self):
        return self.N_images

    def get_filename(self, i):
        return f"{self.root}/{self.image_list[i][:-1]}"


class InstreDataset:
    def __init__(self, dataset_path):
        self.base_dir = dataset_path
        gnd_instre = scipy.io.loadmat(os.path.join(self.base_dir, "gnd_instre.mat"))
        self.gnd = gnd_instre["gnd"][0]
        self.qimlist = [fname[0] for fname in gnd_instre["qimlist"][0]]
        self.db_imlist = [fname[0] for fname in gnd_instre["imlist"][0]]
        self.N_images = len(self.db_imlist)
        self.N_queries = len(self.qimlist)

        rs = np.random.RandomState(123)
        nq = self.N_queries
        self.val_subset = set(rs.choice(nq, nq // 10))
        logging.info(
            f"Loaded INSTRE dataset: {self.N_images}, queries: {self.N_queries}"
        )

    def get_num_images(self):
        return self.N_images

    def get_num_query_images(self):
        return self.N_queries

    def get_filename(self, i):
        return f"{self.base_dir}/{self.db_imlist[i]}"

    def get_query_filename(self, i):
        return f"{self.base_dir}/{self.qimlist[i]}"

    def get_query_roi(self, i):
        # INSTRE dataset has no notion of ROI so we return None
        return None

    def eval_from_ranks(self, ranks):
        nq, nb = ranks.shape
        gnd = self.gnd
        sum_ap = 0
        sum_ap_val = 0
        for i in range(nq):
            positives = gnd[i][0][0] - 1
            ok = np.zeros(nb, dtype=bool)
            ok[positives] = True
            pos = np.where(ok[ranks[i]])[0]
            ap = score_ap_from_ranks_1(pos, len(positives))
            sum_ap += ap
            if i in self.val_subset:
                sum_ap_val += ap
        return sum_ap / nq, sum_ap_val / len(self.val_subset)

    def score(self, scores, temp_dir, verbose=True):
        ranks = scores.argsort(axis=1)[:, ::-1]
        mAP, mAP_val = self.eval_from_ranks(ranks)
        if verbose:
            logging.info(f"INSTRE mAP={mAP} val {mAP_val}")
        return mAP, mAP_val


class RevisitedInstanceRetrievalDataset:
    def __init__(self, dataset, dir_main):
        # Credits: https://github.com/filipradenovic/revisitop/blob/master/python/dataset.py#L6     # NOQA

        self.DATASETS = ["roxford5k", "rparis6k"]
        dataset = dataset.lower()
        assert is_revisited_dataset(dataset), f"Unknown dataset: {dataset}!"

        # loading imlist, qimlist, and gnd, in cfg as a dict
        gnd_fname = os.path.join(dir_main, dataset, f"gnd_{dataset}.pkl")
        with open(gnd_fname, "rb") as f:
            cfg = pickle.load(f)
        cfg["gnd_fname"] = gnd_fname
        cfg["ext"] = ".jpg"
        cfg["qext"] = ".jpg"

        cfg["dir_data"] = os.path.join(dir_main, dataset)
        cfg["dir_images"] = os.path.join(cfg["dir_data"], "jpg")

        cfg["n"] = len(cfg["imlist"])
        cfg["nq"] = len(cfg["qimlist"])

        cfg["dataset"] = dataset
        self.cfg = cfg
        logging.info(
            f"Dataset: {dataset}, images: {self.get_num_images()}, "
            f"queries: {self.get_num_query_images()}"
        )

    def get_filename(self, i):
        return os.path.join(
            self.cfg["dir_images"], self.cfg["imlist"][i] + self.cfg["ext"]
        )

    def get_query_filename(self, i):
        return os.path.join(
            self.cfg["dir_images"], self.cfg["qimlist"][i] + self.cfg["qext"]
        )

    def get_num_images(self):
        return self.cfg["n"]

    def get_num_query_images(self):
        return self.cfg["nq"]

    def get_query_roi(self, i):
        return self.cfg["gnd"][i]["bbx"]

    def score(self, sim, temp_dir):
        sim = sim.T
        # Credits: https://github.com/filipradenovic/revisitop/blob/master/python/example_evaluate.py  # NOQA
        ranks = np.argsort(-sim, axis=0)
        # revisited evaluation
        gnd = self.cfg["gnd"]
        # evaluate ranks
        ks = [1, 5, 10]

        # search for easy
        gnd_t = []
        for i in range(len(gnd)):
            g = {}
            g["ok"] = np.concatenate([gnd[i]["easy"]])
            g["junk"] = np.concatenate([gnd[i]["junk"], gnd[i]["hard"]])
            gnd_t.append(g)
        mapE, apsE, mprE, prsE = compute_map(ranks, gnd_t, ks)

        # search for easy & hard
        gnd_t = []
        for i in range(len(gnd)):
            g = {}
            g["ok"] = np.concatenate([gnd[i]["easy"], gnd[i]["hard"]])
            g["junk"] = np.concatenate([gnd[i]["junk"]])
            gnd_t.append(g)
        mapM, apsM, mprM, prsM = compute_map(ranks, gnd_t, ks)

        # search for hard
        gnd_t = []
        for i in range(len(gnd)):
            g = {}
            g["ok"] = np.concatenate([gnd[i]["hard"]])
            g["junk"] = np.concatenate([gnd[i]["junk"], gnd[i]["easy"]])
            gnd_t.append(g)
        mapH, apsH, mprH, prsH = compute_map(ranks, gnd_t, ks)

        logging.info(
            ">> {}: mAP E: {}, M: {}, H: {}".format(
                self.cfg["dataset"],
                np.around(mapE * 100, decimals=2),
                np.around(mapM * 100, decimals=2),
                np.around(mapH * 100, decimals=2),
            )
        )
        logging.info(
            ">> {}: mP@k{} E: {}, M: {}, H: {}".format(
                self.cfg["dataset"],
                np.array(ks),
                np.around(mprE * 100, decimals=2),
                np.around(mprM * 100, decimals=2),
                np.around(mprH * 100, decimals=2),
            )
        )


# Credits: https://github.com/facebookresearch/deepcluster/blob/master/eval_retrieval.py    # NOQA
# Adapted by: Priya Goyal (prigoyal@fb.com)
class InstanceRetrievalImageLoader:
    def __init__(self, S, transforms):
        self.S = S
        self.transforms = transforms

    def apply_img_transform(self, im):
        im_size_hw = np.array((im.size[1], im.size[0]))
        if self.S == -1:
            ratio = 1.0
        elif self.S == -2:
            if np.max(im_size_hw) > 124:
                ratio = 1024.0 / np.max(im_size_hw)
            else:
                ratio = -1
        else:
            ratio = float(self.S) / np.max(im_size_hw)
        new_size = tuple(np.round(im_size_hw * ratio).astype(np.int32))
        im_resized = self.transforms(
            im.resize((new_size[1], new_size[0]), Image.BILINEAR)
        )
        return im_resized, ratio

    def load_and_prepare_whitening_image(self, fname):
        im = Image.open(fname)
        if im.mode != "RGB":
            im = im.convert(mode="RGB")
        if self.transforms is not None:
            im = self.transforms(im)
        return im

    def load_and_prepare_instre_image(self, fname):
        im = Image.open(fname)
        if self.transforms is not None:
            im = self.transforms(im)
        return im

    def load_and_prepare_image(self, fname, roi=None):
        # Read image, get aspect ratio, and resize such as the largest side equals S
        im = Image.open(fname)
        im_resized, ratio = self.apply_img_transform(im)
        # If there is a roi, adapt the roi to the new size and crop. Do not rescale
        # the image once again
        if roi is not None:
            # ROI format is (xmin,ymin,xmax,ymax)
            roi = np.round(roi * ratio).astype(np.int32)
            im_resized = im_resized[:, roi[1] : roi[3], roi[0] : roi[2]]
        return im_resized

    def load_and_prepare_revisited_image(self, img_path, roi=None):
        # to avoid crashing for truncated (corrupted images)
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        # open path as file to avoid ResourceWarning
        # (https://github.com/python-pillow/Pillow/issues/835)
        with open(img_path, "rb") as f:
            img = Image.open(f).convert("RGB")
        if roi is not None:
            im_resized = img.crop(roi)
        im_resized, _ = self.apply_img_transform(img)
        return im_resized


# Credits: https://github.com/facebookresearch/deepcluster/blob/master/eval_retrieval.py    # NOQA
# Adapted by: Priya Goyal (prigoyal@fb.com)
class InstanceRetrievalDataset:
    def __init__(self, path, eval_binary_path):
        self.path = path
        self.eval_binary_path = eval_binary_path
        # Some images from the Paris dataset are corrupted. Standard practice is
        # to ignore them.
        # See: https://www.robots.ox.ac.uk/~vgg/data/parisbuildings/corrupt.txt
        self.blacklisted_images = [
            "paris_louvre_000136",
            "paris_louvre_000146",
            "paris_moulinrouge_000422",
            "paris_museedorsay_001059",
            "paris_notredame_000188",
            "paris_pantheon_000284",
            "paris_pantheon_000960",
            "paris_pantheon_000974",
            "paris_pompidou_000195",
            "paris_pompidou_000196",
            "paris_pompidou_000201",
            "paris_pompidou_000467",
            "paris_pompidou_000640",
            "paris_sacrecoeur_000299",
            "paris_sacrecoeur_000330",
            "paris_sacrecoeur_000353",
            "paris_triomphe_000662",
            "paris_triomphe_000833",
            "paris_triomphe_000863",
            "paris_triomphe_000867",
        ]
        self.blacklisted = set(self.blacklisted_images)
        self.q_names = None
        self.q_index = None
        self.N_images = None
        self.N_queries = None
        self.q_roi = None
        self.load()

    def get_num_images(self):
        return self.N_images

    def get_num_query_images(self):
        return self.N_queries

    def load(self):
        # Load the dataset GT
        self.lab_root = f"{self.path}/lab/"
        self.img_root = f"{self.path}/jpg/"
        logging.info(f"Loading data: {self.path}")
        lab_filenames = np.sort(os.listdir(self.lab_root))
        # Get the filenames without the extension
        self.img_filenames = [
            e[:-4]
            for e in np.sort(os.listdir(self.img_root))
            if e[:-4] not in self.blacklisted
        ]

        # Parse the label files. Some challenges as filenames do not correspond
        # exactly to query names. Go through all the labels to:
        # i) map names to filenames and vice versa
        # ii) get the relevant regions of interest of the queries,
        # iii) get the indexes of the dataset images that are queries
        # iv) get the relevants / non-relevants list
        self.relevants = {}
        self.junk = {}
        self.non_relevants = {}

        self.filename_to_name = {}
        self.name_to_filename = OrderedDict()
        self.q_roi = {}
        for e in lab_filenames:
            if e.endswith("_query.txt"):
                q_name = e[: -len("_query.txt")]
                with open(f"{self.lab_root}/{e}") as fopen:
                    q_data = fopen.readline().split(" ")
                if q_data[0].startswith("oxc1_"):
                    q_filename = q_data[0][5:]
                else:
                    q_filename = q_data[0]
                self.filename_to_name[q_filename] = q_name
                self.name_to_filename[q_name] = q_filename
                with open(f"{self.lab_root}/{q_name}_ok.txt") as fopen:
                    good = {e.strip() for e in fopen}
                with open(f"{self.lab_root}/{q_name}_good.txt") as fopen:
                    good = good.union({e.strip() for e in fopen})
                with open(f"{self.lab_root}/{q_name}_junk.txt") as fopen:
                    junk = {e.strip() for e in fopen}
                good_plus_junk = good.union(junk)
                self.relevants[q_name] = [
                    i
                    for i in range(len(self.img_filenames))
                    if self.img_filenames[i] in good
                ]
                self.junk[q_name] = [
                    i
                    for i in range(len(self.img_filenames))
                    if self.img_filenames[i] in junk
                ]
                self.non_relevants[q_name] = [
                    i
                    for i in range(len(self.img_filenames))
                    if self.img_filenames[i] not in good_plus_junk
                ]
                self.q_roi[q_name] = np.array(
                    [float(q) for q in q_data[1:]], dtype=np.float32
                )

        self.q_names = list(self.name_to_filename.keys())
        self.q_index = np.array(
            [self.img_filenames.index(self.name_to_filename[qn]) for qn in self.q_names]
        )
        self.N_images = len(self.img_filenames)
        self.N_queries = len(self.q_index)

    def score(self, sim, temp_dir):
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        idx = np.argsort(sim, axis=1)[:, ::-1]
        maps = [
            self.score_rnk_partial(i, idx[i], temp_dir)
            for i in range(len(self.q_names))
        ]
        for i in range(len(self.q_names)):
            logging.info("{0}: {1:.2f}".format(self.q_names[i], 100 * maps[i]))
        logging.info(20 * "-")
        logging.info("Mean: {0:.2f}".format(100 * np.mean(maps)))

    def score_rnk_partial(self, i, idx, temp_dir):
        rnk = np.array(self.img_filenames)[idx]
        with open("{0}/{1}.rnk".format(temp_dir, self.q_names[i]), "w") as f:
            f.write("\n".join(rnk) + "\n")
        cmd = (
            f"{self.eval_binary_path} {self.lab_root}{self.q_names[i]} "
            f"{temp_dir}/{self.q_names[i]}.rnk"
        )
        p = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        map_ = float(p.stdout.readlines()[0])
        p.wait()
        return map_

    def get_filename(self, i):
        return os.path.normpath(
            "{0}/{1}.jpg".format(self.img_root, self.img_filenames[i])
        )

    def get_query_filename(self, i):
        return os.path.normpath(
            f"{self.img_root}/{self.img_filenames[self.q_index[i]]}.jpg"
        )

    def get_query_roi(self, i):
        return self.q_roi[self.q_names[i]]