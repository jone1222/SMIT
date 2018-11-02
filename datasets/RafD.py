import torch
import os
import random
from torch.utils.data import Dataset
from PIL import Image
import ipdb
import numpy as np
import glob  
from misc.utils import _horovod
hvd = _horovod()   

######################################################################################################
###                                              RafD                                              ###
######################################################################################################
class RafD(Dataset):
  def __init__(self, image_size, metadata_path, transform, mode, shuffling=False, RafD_EMOTIONS=False, RafD_FRONTAL=False, **kwargs):
    self.transform = transform
    self.image_size = image_size
    self.shuffling = shuffling
    self.name = 'RafD'
    self.FRONTAL = RafD_FRONTAL
    self.EMOTIONS = RafD_EMOTIONS
    data_root = os.path.join('data', 'RafD', '{}')
    data_root = data_root.format('faces') if 'faces' in metadata_path else data_root.format('data')
    self.lines = sorted(glob.glob(os.path.abspath(os.path.join(data_root,'*.jpg'))))
    self.mode = 'train' if mode=='train' else 'test'
    self.lines = self.get_subjects(self.lines, mode)
    if mode!='val' and hvd.rank() == 0: print ('Start preprocessing %s: %s!'%(self.name, mode))
    random.seed(1234)
    self.preprocess()
    if mode!='val' and hvd.rank() == 0: print ('Finished preprocessing %s: %s (%d)!'%(self.name, mode, self.num_data))

  def preprocess(self):
    self.pose = [0,45,90,135,180]

    if self.FRONTAL or self.EMOTIONS: 
      self.selected_attrs = ['neutral', 'angry', 'contemptuous', 'disgusted', 'fearful', 'happy', 'sad', 'surprised']
      index = 0
    else:
      self.selected_attrs = ['pose_0', 'pose_45', 'pose_90', 'pose_135', 'pose_180',
                    'neutral', 'angry', 'contemptuous', 'disgusted', 'fearful', 'happy', 'sad', 'surprised']
      index = 5
    self.idx2cls = {idx:key for idx, key in enumerate(self.selected_attrs)}
    self.cls2idx = {key:idx for idx, key in enumerate(self.selected_attrs)}    
    self.filenames = []
    self.labels = []

    lines = self.lines
    if self.shuffling: random.shuffle(lines) 
    for i, line in enumerate(lines):
      _class = os.path.basename(line).split('_')[-2]
      pose = int(os.path.basename(line).split('_')[0].replace('Rafd',''))
      if self.FRONTAL and pose!=90 and not self.EMOTIONS: continue
      if (self.EMOTIONS and pose==0) or (self.EMOTIONS and pose==180): continue
      # label = [pose, self.cls2idx[_class]]
      label = []
      if not self.FRONTAL and not self.EMOTIONS:
        for _pose in self.pose:
          if _pose == pose:
            label.append(1)
          else:
            label.append(0)

      for value in self.selected_attrs[index:]:
        if _class == value:
          label.append(1)
        else:
          label.append(0)

      self.filenames.append(line)
      self.labels.append(label)
      # ipdb.set_trace()
    self.num_data = len(self.filenames)

  def get_data(self):
    return self.filenames, self.labels

  def __getitem__(self, index):
    image = Image.open(self.filenames[index]).convert('RGB')
    label = self.labels[index]
    return self.transform(image), torch.FloatTensor(label), self.filenames[index]

  def __len__(self):
    return self.num_data    

  def shuffle(self, seed):
    random.seed(seed)
    random.shuffle(self.filenames)
    random.seed(seed)
    random.shuffle(self.labels)   

  def get_subjects(self, lines, mode='train'):
    subjects = sorted(list(set([os.path.basename(line).split('_')[1] for line in lines])))
    split = 10 #90-10
    new_lines = []
    if mode=='train':
      mode_subjects = subjects[:9*len(subjects)/split]
    else:
      mode_subjects = subjects[9*len(subjects)/split:]
    for line in lines:
      subject = os.path.basename(line).split('_')[1]
      if subject in mode_subjects: new_lines.append(line)
    return new_lines


def train_inception(batch_size, \
        shuffling = False, num_workers=4, HOROVOD=False, **kwargs):

  from torchvision.models import inception_v3
  from misc.utils import to_var, to_cuda, to_data
  from torchvision import transforms, datasets
  from torch.utils.data import DataLoader
  import torch.nn.functional as F
  import torch, torch.nn as nn, tqdm, ipdb
  from misc.utils import _horovod
  hvd = _horovod()  
  hvd.init()

  metadata_path = os.path.join('data', 'RafD', 'normal')
  kwargs['RafD_EMOTIONS'] = True
  # inception Norm

  image_size = 299

  transform = []
  window = int(image_size/10)
  transform+=[transforms.Resize((image_size+window, image_size+window), interpolation=Image.ANTIALIAS)]
  transform+=[transforms.RandomResizedCrop(image_size, scale=(0.7, 1.0), ratio=(0.8, 1.2))]
  transform+=[transforms.RandomHorizontalFlip()]
  transform+=[transforms.ToTensor()]  
  transform = transforms.Compose(transform)

  dataset_train = RafD(image_size, metadata_path, transform, 'train', shuffling=True, **kwargs)
  dataset_test = RafD(image_size, metadata_path, transform, 'test', shuffling=False, **kwargs)
  if not HOROVOD:
    train_loader = DataLoader(dataset=dataset_train, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    test_loader = DataLoader(dataset=dataset_test, batch_size=batch_size, shuffle=False, num_workers=num_workers)
  else:
    data_sampler = torch.utils.data.distributed.DistributedSampler(dataset_train, num_replicas=hvd.size(), rank=hvd.rank())    
    train_loader = DataLoader(dataset_train, batch_size=batch_size, sampler=data_sampler, shuffle=False, num_workers=num_workers)
    data_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test, num_replicas=hvd.size(), rank=hvd.rank())    
    test_loader = DataLoader(dataset_test, batch_size=batch_size, sampler=data_sampler, shuffle=False, num_workers=num_workers)    

  num_labels = len(train_loader.dataset.labels[0])
  n_epochs = 100
  net = inception_v3(pretrained=True, transform_input=True)
  net.aux_logits = False
  num_ftrs = net.fc.in_features
  net.fc = nn.Linear(num_ftrs, num_labels) 
  
  net_save = metadata_path+'/inception_v3/{}.pth'
  if not os.path.isdir(os.path.dirname(net_save)): os.makedirs(os.path.dirname(net_save))
  print("Model will be saved at: "+net_save)
  optimizer = torch.optim.RMSprop(net.parameters(), lr=1e-5)
  # loss = F.cross_entropy(output, target)
  to_cuda(net)
  
  for epoch in range(n_epochs):
    LOSS={'train': [], 'test':[]}; OUTPUT = {'train':[], 'test':[]}; LABEL = {'train':[], 'test':[]}

    net.eval()
    for i, (data, label, files) in tqdm.tqdm(enumerate(test_loader), total=len(test_loader), desc='Validating Inception V3 | RafD'):
      data = to_var(data, volatile=True)
      label = to_var(torch.max(label, dim=1)[1], volatile=True)
      out = net(data)
      loss = F.cross_entropy(out, label)
      # ipdb.set_trace()
      LOSS['test'].append(to_data(loss, cpu=True)[0])
      OUTPUT['test'].extend(to_data(F.softmax(out, dim=1).max(1)[1], cpu=True).tolist())
      LABEL['test'].extend(to_data(label, cpu=True).tolist())
    acc_test = (np.array(OUTPUT['test'])==np.array(LABEL['test'])).mean()
    print('[Test] Loss: {:.4f} Acc: {:.4f}'.format(np.array(LOSS['test']).mean(), acc_test))

    net.train()
    for i, (data, label, files) in tqdm.tqdm(enumerate(train_loader), total=len(train_loader), desc='[{}/{}] Train Inception V3 | RafD'.format(str(epoch).zfill(5), str(n_epochs).zfill(5))):
      # ipdb.set_trace()
      data = to_var(data)
      label = to_var(torch.max(label, dim=1)[1])
      out = net(data)
      # ipdb.set_trace()
      loss = F.cross_entropy(out, label)
      optimizer.zero_grad()
      loss.backward()
      optimizer.step()       
      LOSS['train'].append(to_data(loss, cpu=True)[0])
      OUTPUT['train'].extend(to_data(F.softmax(out, dim=1).max(1)[1], cpu=True).tolist())
      LABEL['train'].extend(to_data(label, cpu=True).tolist())

    
    acc_train = (np.array(OUTPUT['train'])==np.array(LABEL['train'])).mean()
    print('[Train] Loss: {:.4f} Acc: {:.4f}'.format(np.array(LOSS['train']).mean(), acc_train))
    torch.save(net.state_dict(), net_save.format(str(epoch).zfill(5)))
    train_loader.dataset.shuffle(epoch)

