import torch
import os
import random
from torch.utils.data import Dataset
from PIL import Image
import glob
from misc.utils import PRINT

# ==================================================================#
# == Image2Edges
# ==================================================================#


class Image2Edges(Dataset):
    def __init__(self,
                 image_size,
                 mode_data,
                 transform,
                 mode,
                 shuffling=False,
                 all_attr=-1,
                 verbose=False,
                 **kwargs):
        self.transform = transform
        self.image_size = image_size
        self.shuffling = shuffling
        self.name = 'Image2Edges'
        self.all_attr = all_attr
        self.mode_data = mode_data
        self.verbose = verbose
        self.mode = mode
        mode = mode if mode == 'train' else 'val'

        self.lines = sorted(
            glob.glob('data/{}/edges2*/{}/*__*.jpg'.format(self.name, mode)))
        self.attr2idx = {
            self.key_fn(line): idx
            for idx, line in enumerate(self.lines)
        }
        self.idx2attr = {
            idx: self.key_fn(line)
            for idx, line in enumerate(self.lines)
        }
        if self.verbose:
            print('Start preprocessing %s: %s!' % (self.name, mode))
        random.seed(1234)
        self.preprocess()
        if self.verbose:
            print('Finished preprocessing %s: %s (%d)!' % (self.name, mode,
                                                           self.num_data))

    def key_fn(self, line):
        return line.split('/')[-1].split('__')[1].split('.')[0]

    def histogram(self):
        self.hist = {key: 0 for key in self.attr2idx.keys()}
        for line in self.lines:
            key = self.key_fn(line)
            self.hist[key] += 1
        total = 0
        with open('datasets/{}_histogram_attributes.txt'.format(self.name),
                  'w') as f:
            for key, value in sorted(
                    self.hist.items(), key=lambda kv: (kv[1], kv[0]),
                    reverse=True):
                total += value
                PRINT(f, '{} {}'.format(key, value))
            PRINT(f, 'TOTAL {}'.format(total))

    def preprocess(self):
        if self.verbose:
            self.histogram()
        if self.all_attr == 2:
            self.selected_attrs = [
                key for key, value in sorted(
                    self.attr2idx.items(), key=lambda kv: (kv[1], kv[0]))
            ]  # self.attr2idx.keys()
        elif self.all_attr == 1:
            self.selected_attrs = ['Edges_Handbags', 'Image_Handbags']
        else:
            self.selected_attrs = ['Edges_Shoes', 'Image_Shoes']
        self.filenames = []
        self.labels = []

        if self.shuffling:
            random.shuffle(self.lines)
        balanced = {key: 0 for key in self.selected_attrs}
        for i, line in enumerate(self.lines):
            filename = os.path.abspath(line)
            key = self.key_fn(line)
            if key not in self.selected_attrs:
                continue
            if self.mode == 'train' and self.all_attr == 0 and balanced[
                    key] >= min(self.hist.values()):
                continue  # Balancing all classes to the minimum
            balanced[key] += 1
            label = []
            for attr in self.selected_attrs:
                if attr == key:
                    label.append(1)
                else:
                    label.append(0)

            self.filenames.append(filename)
            self.labels.append(label)

        self.num_data = len(self.filenames)

    def get_data(self):
        return self.filenames, self.labels

    def __getitem__(self, index):
        image = Image.open(self.filenames[index]).convert('RGB')
        label = self.labels[index]
        return self.transform(image), torch.FloatTensor(
            label), self.filenames[index]

    def __len__(self):
        return self.num_data

    def shuffle(self, seed):
        random.seed(seed)
        random.shuffle(self.filenames)
        random.seed(seed)
        random.shuffle(self.labels)


if __name__ == '__main__':
    # mpirun -np 10 ipython datasets/Image2Edges.py
    from tqdm import tqdm
    lines = sorted(glob.glob('data/Image2Edges/edges2*/*/*AB.jpg'))
    for line in tqdm(lines, total=len(lines), desc='Spliting images'):
        edges_file = line.replace(
            'AB', '_Edges_{}'.format(
                line.split('/')[-3].split('2')[1].capitalize()))
        image_file = line.replace(
            'AB', '_Image_{}'.format(
                line.split('/')[-3].split('2')[1].capitalize()))
        img = Image.open(line)

        image = img.crop((256, 0, 512, 256))
        edges = img.crop((0, 0, 256, 256))
        edges.save(edges_file)
        image.save(image_file)
