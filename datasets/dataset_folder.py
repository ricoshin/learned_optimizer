import os
import sys
from collections import OrderedDict as odict
from os import path

import numpy as np
import torch
from datasets.dataset_helpers import Metadata
from PIL import Image
from torch.utils import data


def has_file_allowed_extension(filename, extensions):
  """Checks if a file is an allowed extension.

  Args:
      filename (string): path to a file
      extensions (tuple of strings): extensions to consider (lowercase)

  Returns:
      bool: True if the filename ends with one of given extensions
  """
  return filename.lower().endswith(extensions)


def is_image_file(filename):
  """Checks if a file is an allowed image extension.

  Args:
      filename (string): path to a file

  Returns:
      bool: True if the filename ends with a known image extension
  """
  return has_file_allowed_extension(filename, IMG_EXTENSIONS)


def make_dataset(dir, class_to_idx, extensions=None, is_valid_file=None):
  idx_to_samples = odict()
  dir = os.path.expanduser(dir)
  if not ((extensions is None) ^ (is_valid_file is None)):
    raise ValueError("Both extensions and is_valid_file cannot be None "
                     "or not None at the same time")
  if extensions is not None:
    def is_valid_file(x):
      return has_file_allowed_extension(x, extensions)
  for target in sorted(class_to_idx.keys()):
    samples = []
    d = os.path.join(dir, target)
    idx = class_to_idx[target]
    if not os.path.isdir(d):
      continue
    for root, _, fnames in sorted(os.walk(d)):
      for fname in sorted(fnames):
        path = os.path.join(root, fname)
        if is_valid_file(path):
          samples.append((path, idx))
    idx_to_samples[idx] = samples
  return idx_to_samples


def pil_loader(path):
  # open path as file to avoid ResourceWarning
  #   (https://github.com/python-pillow/Pillow/issues/835)
  with open(path, 'rb') as f:
    img = Image.open(f)
    return img.convert('RGB')


def accimage_loader(path):
  import accimage
  try:
    return accimage.Image(path)
  except IOError:
    # Potentially a decoding problem, fall back to PIL.Image
    return pil_loader(path)


def default_loader(path):
  from torchvision import get_image_backend
  if get_image_backend() == 'accimage':
    return accimage_loader(path)
  else:
    return pil_loader(path)


class VisionDataset(data.Dataset):
  _repr_indent = 4

  def __init__(self, root, transforms=None, transform=None,
               target_transform=None):
    if isinstance(root, torch._six.string_classes):
      root = os.path.expanduser(root)
    self.root = root

    has_transforms = transforms is not None
    has_separate_transform = \
        transform is not None or target_transform is not None
    if has_transforms and has_separate_transform:
      raise ValueError("Only transforms or transform/target_transform can "
                       "be passed as argument")

    # for backwards-compatibility
    self.transform = transform
    self.target_transform = target_transform

    if has_separate_transform:
      transforms = StandardTransform(transform, target_transform)
    self.transforms = transforms

  def __getitem__(self, index):
    raise NotImplementedError

  def __len__(self):
    raise NotImplementedError

  def __repr__(self):
    head = "Dataset " + self.__class__.__name__
    body = ["Number of datapoints: {}".format(self.__len__())]
    if self.root is not None:
      body.append("Root location: {}".format(self.root))
    body += self.extra_repr().splitlines()
    if self.transforms is not None:
      body += [repr(self.transforms)]
    lines = [head] + [" " * self._repr_indent + line for line in body]
    return '\n'.join(lines)

  def _format_transform_repr(self, transform, head):
    lines = transform.__repr__().splitlines()
    return (["{}{}".format(head, lines[0])] +
            ["{}{}".format(" " * len(head), line) for line in lines[1:]])

  def extra_repr(self):
    return ""


class DatasetFolder(VisionDataset):
  """A generic data loader where the samples are arranged in this way: ::

      root/class_x/xxx.ext
      root/class_x/xxy.ext
      root/class_x/xxz.ext

      root/class_y/123.ext
      root/class_y/nsdf3.ext
      root/class_y/asd932_.ext

  Args:
    root (string): Root directory path.
    loader (callable): A function to load a sample given its path.
    extensions (tuple[string]): A list of allowed extensions.
      both extensions and is_valid_file should not be passed.
    transform (callable, optional): A function/transform that takes in
      a sample and returns a transformed version.
      E.g, ``transforms.RandomCrop`` for images.
    target_transform (callable, optional): A function/transform that takes
      in the target and transforms it.
    is_valid_file (callable, optional): A function that takes path of an Image
      file and check if the file is a valid_file(used to check of corrupt files)
      both extensions and is_valid_file should not be passed.

   Attributes:
    classes (list): List of the class names.
    class_to_idx (dict): Dict with items (class_name, class_index).
    samples (list): List of (sample path, class_index) tuples
    targets (list): The class_index value for each image in the dataset
  """

  def __init__(self, root, loader, extensions=None, transform=None,
               target_transform=None, is_valid_file=None, load_metadata=False):
    super(DatasetFolder, self).__init__(root)
    self.transform = transform
    self.target_transform = target_transform

    # to be used as postfix of metadata file. e.g. train or val
    basename = path.basename(root)
    # save/load metadata to/from parent dir
    metapath = path.join(root, os.pardir)

    if load_metadata and Metadata.is_loadable(metapath, basename):
      metadata = Metadata.load(metapath, basename)
    else:
      metadata = self._make_metadata(extensions, is_valid_file)
      if load_metadata:
        metadata.save(metafile, basename)

    self.loader = loader
    self.extensions = extensions
    self.meta = metadata
    # self.classes = metadata.classes
    # self.class_to_idx = metadata.class_to_idx
    # self.idx_to_samples = idx_to_samples
    self._set_samples_n_targets()

  def _set_samples_n_targets(self):
    self.samples = []
    self.targets = []
    for idx, sample in self.meta.idx_to_samples.items():
      self.samples.extend(sample)
      self.targets.extend([idx] * len(sample))

  def _make_metadata(self, extensions, is_valid_file):
    print('Making dataset dictionaries..')
    classes, class_to_idx, idx_to_class = self._find_classes(self.root)
    idx_to_samples = make_dataset(
        self.root, class_to_idx, extensions, is_valid_file)
    if any([len(v) == 0 for v in idx_to_samples.values()]):
      raise (RuntimeError(
          "Found 0 files in subfolders of: " + self.root + "\n"
          "Supported extensions are: " + ",".join(extensions)))
    print('Done!')
    return Metadata(classes, class_to_idx, idx_to_class, idx_to_samples)

  def _find_classes(self, dir):
    """
    Finds the class folders in a dataset.

    Args:
        dir (string): Root directory path.

    Returns:
        tuple: (classes, class_to_idx) where classes are relative to (dir),
          and class_to_idx is a dictionary.

    Ensures:
        No class is a subdirectory of another.
    """
    if sys.version_info >= (3, 5):
        # Faster and available in Python 3.5 and above
      classes = [d.name for d in os.scandir(dir) if d.is_dir()]
    else:
      classes = [d for d in os.listdir(
          dir) if os.path.isdir(os.path.join(dir, d))]
    classes.sort()
    class_to_idx = odict({classes[i]: i for i in range(len(classes))})
    idx_to_class = odict({i: classes[i] for i in range(len(classes))})
    return classes, class_to_idx, idx_to_class

  def __getitem__(self, index):
    """
    Args:
        index (int): Index

    Returns:
        tuple: (sample, target) where target is class_index of the target class.
    """
    path, target = self.samples[index]
    sample = self.loader(path)
    if self.transform is not None:
      sample = self.transform(sample)
    if self.target_transform is not None:
      target = self.target_transform(target)

    return sample, target

  def __len__(self):
    return len(self.samples)


IMG_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.ppm', '.bmp',
                  '.pgm', '.tif', '.tiff', '.webp')


class ImageFolder(DatasetFolder):
  """A generic data loader where the images are arranged in this way: ::

      root/dog/xxx.png
      root/dog/xxy.png
      root/dog/xxz.png

      root/cat/123.png
      root/cat/nsdf3.png
      root/cat/asd932_.png

  Args:
    root (string): Root directory path.
    transform (callable, optional): A function/transform that  takes in an PIL
      image and returns a transformed version. E.g, ``transforms.RandomCrop``
    target_transform (callable, optional): A function/transform that takes in the
      target and transforms it.
    loader (callable, optional): A function to load an image given its path.
    is_valid_file (callable, optional): A function that takes path of an Image
      file and check if the file is a valid_file (used to check of corrupt
      files)

   Attributes:
    classes (list): List of the class names.
    class_to_idx (dict): Dict with items (class_name, class_index).
    imgs (list): List of (image path, class_index) tuples
  """

  def __init__(self, root, transform=None, target_transform=None,
               loader=default_loader, is_valid_file=None):
    extensions = IMG_EXTENSIONS if is_valid_file is None else None
    super(ImageFolder, self).__init__(root=root,
                                      loader=loader,
                                      extensions=extensions,
                                      transform=transform,
                                      target_transform=target_transform,
                                      is_valid_file=is_valid_file)
    self.imgs = self.samples
