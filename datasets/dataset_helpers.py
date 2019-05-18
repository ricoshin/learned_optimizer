import pickle
from os import path

import numpy as np
from torch.utils.data import Dataset, dataset


class Metadata(object):
  _filename = 'metadata.pickle'

  def __init__(self, classes, class_to_idx, idx_to_class, idx_to_samples):
    self.classes = classes
    self.class_to_idx = class_to_idx
    self.idx_to_class = idx_to_class
    self.idx_to_samples = idx_to_samples

  def __len__(self):
    return len(self.classes)

  def __eq__(self, other):
    return set(self.classes) == set(other.classes)

  def __add__(self, other):
    return self.merge([self, other])

  def relative_index(self, rel_indices):
    assert isinstance(rel_indices, (tuple, list))
    abs_indices = list(self.idx_to_samples.keys())
    indices = [abs_indices[rel_idx] for rel_idx in rel_indices]
    classes = [self.idx_to_class[idx] for idx in indices]
    class_to_idx = {cls_: self.class_to_idx[cls_] for cls_ in classes}
    idx_to_class = {self.class_to_idx[cls_]: cls_ for cls_ in classes}
    idx_to_samples = {idx: self.idx_to_samples[idx] for idx in indices}
    return Metadata(classes, class_to_idx, idx_to_class, idx_to_samples)

  @classmethod
  def merge(cls, others):
    assert len(others) > 1
    # assert all([others[0] == other for other in others])
    classes = [set(other.classes) for other in others]
    classes = list(classes[0].union(*classes[1:]))
    # import pdb; pdb.set_trace()
    classes.sort()
    class_to_idx = {classes[i]: i for i in range(len(classes))}
    idx_to_class = {i: classes[i] for i in range(len(classes))}
    idx_to_samples = {}
    for idx, class_ in idx_to_class.items():
      samples = []
      for other in others:
        samples.extend(other.idx_to_samples[idx])
      idx_to_samples[idx] = list(set(samples))
    return cls(classes, class_to_idx, idx_to_class, idx_to_samples)

  @classmethod
  def is_loadable(cls, root):
    filepath = path.join(root, cls._filename)
    return path.exists(filepath)

  @classmethod
  def load(cls, root):
    filepath = path.join(root, cls._filename)
    with open(filepath, 'rb') as f:
      meta_data = pickle.load(f)
    print(f'Loaded preprocessed dataset dictionaries: {filepath}')
    return meta_data

  def save(self, root):
    filepath = path.join(root, self._filename)
    with open(filepath, 'wb') as f:
      pickle.dump(self, f)
    print(f'Saved processed dataset dictionaries: {filepath}')


class ConcatDatasetFolder(dataset.ConcatDataset):
  """Dataset to concatenate multiple 'DatasetFolder's"""

  def __init__(self, datasets):
    super(ConcatDatasetFolder, self).__init__(datasets)
    # if not all([isinstance(dataset, DatasetFolder) for dataset in datasets]):
    #   raise TypeError('All the datasets have to be DatasetFolders.')
    # assert all([others[0] == dataset.metadata for dataset in datasets])
    self.metadata = Metadata.merge([dset.metadata for dset in self.datasets])


class SubsetClass(Dataset):
  def __init__(self, dataset, idx=None, debug=False):
    self.valid_dataset(dataset)
    self.dataset = dataset
    if idx is None:
      self.indices = list(range(len(dataset)))
      self.metadata = dataset.metadata
    else:
      prev = 0
      self.indices = []
      self.idxx = []
      self.metadata = dataset.metadata.relative_index(idx)

      for k, v in self.metadata.idx_to_samples.items():
        curr = prev + len(v)
        self.indices.extend(list(range(prev, curr)))
        # self.idxx.append(list(range(prev, curr)))
        # import pdb; pdb.set_trace()
        prev = curr
      # if debug:
      #   import pdb; pdb.set_trace()

  def __getitem__(self, idx):
    # return self.dataset[idx]
    return self.dataset[self.indices[idx]]

  def __len__(self):
    return len(self.indices)

  @staticmethod
  def valid_dataset(dataset):
    assert isinstance(dataset, Dataset)
    if not (hasattr(dataset, 'metadata') and
            isinstance(dataset.metadata, Metadata)):
      import pdb; pdb.set_trace()
      raise Exception("Dataset should have attributes 'metadata', instance of "
                      "datasets.dataset_helper.Metadata.")

  @classmethod
  def random_sample(cls, dataset, num):
    cls.valid_dataset(dataset)
    sampled_idx = np.random.choice(
      len(dataset.metadata.classes), num, replace=False)
    return cls(dataset, sampled_idx.tolist(), debug=True)

  @classmethod
  def split(cls, dataset, ratio):
    cls.valid_dataset(dataset)
    n_classes = len(dataset.metadata.classes)
    idx = list(range(n_classes))
    thres = int(n_classes * ratio)
    return cls(dataset, idx[:thres]), cls(dataset, idx[thres:])
