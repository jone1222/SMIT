import torch, os, time, ipdb, glob, math, warnings, datetime
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torchvision.utils import save_image
import config as cfg
from tqdm import tqdm
from termcolor import colored
from misc.utils import create_dir, denorm, get_aus, get_loss_value, make_gif, PRINT, send_mail, target_debug_list, TimeNow, TimeNow_str, to_cuda, to_data, to_var
from misc.losses import _compute_kl, _compute_loss_smooth, _compute_vgg_loss, _GAN_LOSS, _get_gradient_penalty
warnings.filterwarnings('ignore')

class Solver(object):

  def __init__(self, config, data_loader=None):
    # Data loader
    self.data_loader = data_loader
    self.config = config

    # Build tensorboard if use
    self.build_model()
    if self.config.use_tensorboard:
      self.build_tensorboard()

    # Start with trained model
    if self.config.pretrained_model:
      self.load_pretrained_model()

  #=======================================================================================#
  #=======================================================================================#
  def build_model(self):
    # Define a generator and a discriminator
    if self.config.MultiDis>0:
      from model import MultiDiscriminator as Discriminator
    else:
      from model import Discriminator
    if 'AdaIn' in self.config.GAN_options and 'Stochastic' not in self.config.GAN_options:
      from model import AdaInGEN_Label as GEN
    elif 'AdaIn' in self.config.GAN_options:
      if 'DRIT' not in self.config.GAN_options: from model import AdaInGEN as GEN
      else: from model import DRITGEN as GEN
    elif 'DRITZ' in self.config.GAN_options: from model import DRITZGEN as GEN
    else: from model import Generator as GEN
    self.G = GEN(self.config, debug=self.config.mode=='train')

    G_parameters = self.G.parameters()
    self.g_optimizer = torch.optim.Adam(G_parameters, self.config.g_lr, [self.config.beta1, self.config.beta2])
    to_cuda(self.G)

    self.D = Discriminator(self.config, debug=self.config.mode=='train') 
    if self.config.mode=='train': 
      D_parameters = self.D.parameters()
      self.d_optimizer = torch.optim.Adam(D_parameters, self.config.d_lr, [self.config.beta1, self.config.beta2])
      self.print_network(self.D, 'Discriminator')
      self.print_network(self.G, 'Generator')
    to_cuda(self.D)

    if 'L1_Perceptual' in self.config.GAN_options or 'Perceptual' in self.config.GAN_options:
      import importlib
      perceptual = importlib.import_module('models.perceptual.{}'.format(self.config.PerceptualLoss))
      self.vgg = getattr(perceptual, self.config.PerceptualLoss)()
      to_cuda(self.vgg)
      self.vgg.eval()
      for param in self.vgg.parameters():
          param.requires_grad = False                

  #=======================================================================================#
  #=======================================================================================#

  def print_network(self, model, name):
    if 'AdaIn' in self.config.GAN_options and name=='Generator':
      if 'Stochastic' in self.config.GAN_options:
        choices = ['generator', 'enc_style', 'adain_net']
      else:
        choices = ['generator', 'adain_net']
      if 'DRIT' in self.config.GAN_options: choices.pop(-1)
      for m in choices:
        submodel = getattr(model, m)
        num_params = 0
        for p in submodel.parameters():
          num_params += p.numel()
        self.PRINT("{} number of parameters: {}".format(m.upper(), num_params))
    else:
      num_params = 0
      for p in model.parameters():
        num_params += p.numel()   
      self.PRINT("{} number of parameters: {}".format(name.upper(), num_params))   
    # self.PRINT(name)
    # self.PRINT(model)
    # self.PRINT("{} number of parameters: {}".format(name, num_params))
    # self.display_net(name)

  #=======================================================================================#
  #=======================================================================================#
  def save(self, Epoch, iter):
    name = os.path.join(self.config.model_save_path, '{}_{}_{}.pth'.format(Epoch, iter, '{}'))
    torch.save(self.G.state_dict(), name.format('G'))
    torch.save(self.D.state_dict(), name.format('D'))
    if int(Epoch)>2:
      name_1 = os.path.join(self.config.model_save_path, '{}_{}_{}.pth'.format(str(int(Epoch)-1).zfill(3), iter, '{}'))
      if os.path.isfile(name_1.format('G')): os.remove(name_1.format('G'))
      if os.path.isfile(name_1.format('D')): os.remove(name_1.format('D'))

  #=======================================================================================#
  #=======================================================================================#
  def load_pretrained_model(self):
    name = os.path.join(self.config.model_save_path, '{}_{}.pth'.format(self.config.pretrained_model, '{}'))
    self.G.load_state_dict(torch.load(name.format('G')))#, map_location=lambda storage, loc: storage))
    self.D.load_state_dict(torch.load(name.format('D')))#, map_location=lambda storage, loc: storage))
    self.PRINT('loaded trained models (step: {})..!'.format(self.config.pretrained_model))

  #=======================================================================================#
  #=======================================================================================#
  def resume_name(self):
    if self.config.pretrained_model in ['',None]:
      last_file = sorted(glob.glob(os.path.join(self.config.model_save_path,  '*_G.pth')))[-1]
      last_name = '_'.join(os.path.basename(last_file).split('_')[:2])
    else:
      last_name = self.config.pretrained_model 
    return last_name

  #=======================================================================================#
  #=======================================================================================#
  def build_tensorboard(self):
    from misc.logger import Logger
    self.logger = Logger(self.config.log_path)

  #=======================================================================================#
  #=======================================================================================#
  def update_lr(self, g_lr, d_lr):
    for param_group in self.g_optimizer.param_groups:
      param_group['lr'] = g_lr
    for param_group in self.d_optimizer.param_groups:
      param_group['lr'] = d_lr

  #=======================================================================================#
  #=======================================================================================#
  def reset_grad(self):
    self.g_optimizer.zero_grad()
    self.d_optimizer.zero_grad()

  #=======================================================================================#
  #=======================================================================================#
  def update_loss(self, loss, value):
    try:
      self.LOSS[loss].append(value)
    except:
      self.LOSS[loss] = []
      self.LOSS[loss].append(value)

  #=======================================================================================#
  #=======================================================================================#
  def get_aus(self):
    return get_aus(self.config.image_size, self.config.dataset_fake, attr=self.data_loader.dataset)

  #=======================================================================================#
  #=======================================================================================#
  def color(self, dict, key, color='red'):
    from termcolor import colored
    dict[key] = colored('%.2f'%(dict[key]), color)

  #=======================================================================================#
  #=======================================================================================#
  def get_randperm(self, x):
    if x.size(0)>2:
      rand_idx = to_var(torch.randperm(x.size(0)))
    elif x.size(0)==2:
      rand_idx = to_var(torch.LongTensor([1,0]))
    else:
      rand_idx = to_var(torch.LongTensor([0]))
    return rand_idx

  #=======================================================================================#
  #=======================================================================================#
  def PRINT(self, str):  
    if self.config.mode=='train': PRINT(self.config.log, str)
    else: print(str)

  #=======================================================================================#
  #=======================================================================================#
  def _criterion_style(self, output_style, target_style):
    criterion_style = torch.nn.MSELoss() if 'mse_style' in self.config.GAN_options else torch.nn.L1Loss()
    return criterion_style(output_style, target_style)

  #=======================================================================================#
  #=======================================================================================#
  def _compute_vgg_loss(self, data_x, data_y):
    return _compute_vgg_loss(self.vgg, data_x, data_y)

  #=======================================================================================#
  #=======================================================================================#
  def _CLS(self, data):
    data = to_var(data, volatile=True)
    out_label = self.D(data)[1]
    if len(out_label)>1:
      out_label = torch.cat([F.sigmoid(out.unsqueeze(-1)) for out in out_label], dim=-1).mean(dim=-1)
    else:
      out_label = F.sigmoid(out_label[0])
    out_label = (out_label>0.5).float()    
    return out_label

  #=======================================================================================#
  #=======================================================================================#
  def _SAVE_IMAGE(self, save_path, fake_list, attn_list=[], gif=False, mode='fake'):
    Output = []
    fake_images = denorm(to_data(torch.cat(fake_list, dim=3), cpu=True))
    if gif: make_gif(fake_images, save_path)
    fake_images = torch.cat((self.get_aus(), fake_images), dim=0)
    _save_path = save_path.replace('fake', mode)
    save_image(fake_images, _save_path, nrow=1, padding=0)
    Output.append(_save_path)
    if gif: 
      Output.append(_save_path.replace('jpg', 'gif'))
      Output.append(_save_path.replace('jpg', 'mp4'))
    if len(attn_list): 
      fake_attn = to_data(torch.cat(attn_list, dim=3), cpu=True)
      fake_attn = torch.cat((self.get_aus(), fake_attn), dim=0)
      _save_path = save_path.replace('fake', '{}_attn'.format(mode))
      save_image(fake_attn, _save_path, nrow=1, padding=0)
      Output.append(_save_path.replace('fake', 'attn'))    
    return Output

  #=======================================================================================#
  #=======================================================================================#
  def _GAN_LOSS(self, real_x, fake_x, label, is_fake=False):
    return _GAN_LOSS(self.D, real_x, fake_x, label, self.config.GAN_options, is_fake=is_fake)

  #=======================================================================================#
  #=======================================================================================#
  def _get_gradient_penalty(self, real_x, fake_x):
    return _get_gradient_penalty(self.D, real_x, fake_x)

  #=======================================================================================#
  #=======================================================================================#
  def Disc_update(self, real_x0, real_c0, GAN_options):

    rand_idx0 = self.get_randperm(real_c0)
    fake_c0 = real_c0[rand_idx0]
    fake_c0 = to_var(fake_c0.data)

    ############################# Stochastic Part ##################################
    if 'Stochastic' in GAN_options:
      style_fake0 = [to_var(self.G.random_style(real_x0))]
      if 'style_labels' in GAN_options:
        style_fake0 = [s*fake_c0.unsqueeze(2) for s in style_fake0]
    else:
      style_fake0 = [None]

    fake_x0 = self.G(real_x0, fake_c0, stochastic=style_fake0[0])[0]

    #=======================================================================================#
    #======================================== Train D ======================================#
    #=======================================================================================#
    d_loss_src, d_loss_cls = self._GAN_LOSS(real_x0, fake_x0, real_c0)
    d_loss_cls = self.config.lambda_cls * d_loss_cls  

    # Backward + Optimize       
    d_loss = d_loss_src + d_loss_cls

    self.reset_grad()
    d_loss.backward()
    self.d_optimizer.step()

    self.loss['Dsrc'] = get_loss_value(d_loss_src)
    self.loss['Dcls'] = get_loss_value(d_loss_cls)          
    self.update_loss('Dsrc', self.loss['Dsrc'])
    self.update_loss('Dcls', self.loss['Dcls'])

    #=======================================================================================#
    #=================================== Gradient Penalty ==================================#
    #=======================================================================================#
    # Compute gradient penalty
    if not 'HINGE' in GAN_options:
      d_loss_gp = self._get_gradient_penalty(real_x0.data, fake_x0.data)
      d_loss = self.config.lambda_gp * d_loss_gp
      self.loss['Dgp'] = get_loss_value(d_loss)
      self.update_loss('Dgp', self.loss['Dgp'])
      self.reset_grad()
      d_loss.backward()
      self.d_optimizer.step()     

  #=======================================================================================#
  #=======================================================================================#    
  #=======================================================================================#
  #=======================================================================================#
  def train(self):

    # Fixed inputs and target domain labels for debugging
    opt = torch.no_grad() if int(torch.__version__.split('.')[1])>3 else open('_null.txt', 'w')
    with opt:
      fixed_x = []
      for i, (images, labels, files) in enumerate(self.data_loader):
        fixed_x.append(images)
        if i == max(1,int(16/self.config.batch_size)):
          break
      fixed_x = torch.cat(fixed_x, dim=0)
    
    # lr cache for decaying
    g_lr = self.config.g_lr
    d_lr = self.config.d_lr

    # Start with trained model if exists
    if self.config.pretrained_model:
      start = int(self.config.pretrained_model.split('_')[0])
      for i in range(start):
        if (i+1) %self.config.num_epochs_decay==0:
          g_lr = (g_lr / 10.)
          d_lr = (d_lr / 10.)
          self.update_lr(g_lr, d_lr)
          self.PRINT ('Decay learning rate to g_lr: {}, d_lr: {}.'.format(g_lr, d_lr))     
    else:
      start = 0

    # The number of iterations per epoch
    last_model_step = len(self.data_loader)
    GAN_options = self.config.GAN_options

    self.PRINT("Current time: "+TimeNow())

    # Tensorboard log path
    if self.config.use_tensorboard: self.PRINT("Tensorboard Log path: "+self.config.log_path)
    self.PRINT("Debug Log txt: "+os.path.realpath(self.config.log.name))

    #RaGAN uses different data for Dis and Gen 
    batch_size = self.config.batch_size//2  if 'RaGAN' in GAN_options else self.config.batch_size

    # Log info
    Log = "---> batch size: {}, fold: {}, img: {}, GPU: {}, !{}, [{}]\n-> GAN_options:".format(\
        batch_size, self.config.fold, self.config.image_size, \
        self.config.GPU, self.config.mode_data, self.config.PLACE) 
    for item in sorted(GAN_options):
      Log += ' [*{}]'.format(item.upper())
    Log += ' [*{}]'.format(self.config.dataset_fake)
    self.PRINT(Log)
    start_time = time.time()

    criterion_l1 = torch.nn.L1Loss()
    style_flag = True
    # Start training
    for e in range(start, self.config.num_epochs):
      E = str(e+1).zfill(3)
      self.D.train()
      self.G.train()
      self.LOSS = {}
      desc_bar = 'Epoch: %d/%d'%(e,self.config.num_epochs)
      progress_bar = tqdm(enumerate(self.data_loader), unit_scale=True, 
          total=len(self.data_loader), desc=desc_bar, ncols=5)
      for i, (real_x, real_c, files) in progress_bar: 

        self.loss = {}

        #=======================================================================================#
        #====================================== DATA2VAR =======================================#
        #=======================================================================================#
        # Convert tensor to variable
        real_x = to_var(real_x)
        real_c = to_var(real_c)       

        #RaGAN uses different data for Dis and Gen 
        if 'RaGAN' in GAN_options:
          split = lambda x: (x[:x.size(0)//2], x[x.size(0)//2:])
        else:
          split = lambda x: (x, x)

        real_x0, real_x1 = split(real_x)
        real_c0, real_c1 = split(real_c)          

        rand_idx1 = self.get_randperm(real_c1)
        fake_c1 = real_c1[rand_idx1]
        fake_c1 = to_var(fake_c1.data)

        self.Disc_update(real_x0, real_c0, GAN_options)        
        
        #=======================================================================================#
        #======================================= Train G =======================================#
        #=======================================================================================#
        if (i+1) % self.config.d_train_repeat == 0:

          ############################## Stochastic Part ##################################
          if 'Stochastic' in GAN_options:
            style_fake1 = to_var(self.G.random_style(real_x1))
            style_rec1 = to_var(self.G.random_style(real_x1))
            if 'style_labels' in GAN_options:
              style_rec1 = style_rec1*real_c1.unsqueeze(-1)
              style_fake1 = style_fake1*fake_c1.unsqueeze(-1)     
          else:
            style_fake1 = style_rec1 = None

          fake_x1 = self.G(real_x1, fake_c1, stochastic = style_fake1, CONTENT='content_loss' in GAN_options)

          ## GAN LOSS
          g_loss_src, g_loss_cls = self._GAN_LOSS(fake_x1[0], real_x1, fake_c1, is_fake=True)

          g_loss_cls = g_loss_cls*self.config.lambda_cls
          self.loss['Gsrc'] = get_loss_value(g_loss_src)
          self.loss['Gcls'] = get_loss_value(g_loss_cls)
          self.update_loss('Gsrc', self.loss['Gsrc'])
          self.update_loss('Gcls', self.loss['Gcls'])

          ## REC LOSS
          rec_x1  = self.G(fake_x1[0], real_c1, stochastic = style_rec1, CONTENT='content_loss' in GAN_options) 
          if 'Perceptual' in GAN_options:
            g_loss_recp = self.config.lambda_perceptual*self._compute_vgg_loss(real_x1, rec_x1[0])     
            self.loss['Grecp'] = get_loss_value(g_loss_recp)
            self.update_loss('Grecp', self.loss['Grecp'])

            g_loss_rec = 0.01*self.config.lambda_perceptual*self.config.lambda_rec*criterion_l1(rec_x1[0], real_x1)   
            self.loss['Grec'] = get_loss_value(g_loss_rec) 
            self.update_loss('Grec', self.loss['Grec'])

            g_loss_rec += g_loss_recp

          else:
            g_loss_rec = self.config.lambda_rec*criterion_l1(rec_x1[0], real_x1)
            self.loss['Grec'] = get_loss_value(g_loss_rec) 
            self.update_loss('Grec', self.loss['Grec'])            

          # Backward + Optimize
          g_loss = g_loss_src + g_loss_rec + g_loss_cls 

          ############################## Background Consistency Part ###################################
          if 'L1_LOSS' in GAN_options:
            g_loss_rec1 = self.config.lambda_l1*(criterion_l1(fake_x1[0], real_x1) + criterion_l1(rec_x1[0], fake_x1[0].detach()))
            self.loss['Grec1'] = get_loss_value(g_loss_rec1)
            self.update_loss('Grec1', self.loss['Grec1'])    
            g_loss += g_loss_rec1

          ##############################      L1 Perceptual Part   ###################################
          if 'L1_Perceptual' in GAN_options:
            l1_perceptual = self._compute_vgg_loss(fake_x1[0], real_x1) + self._compute_vgg_loss(rec_x1[0], fake_x1[0])
            g_loss_rec1p = self.config.lambda_l1perceptual*l1_perceptual
            self.loss['Grec1p'] = get_loss_value(g_loss_rec1p)
            self.update_loss('Grec1p', self.loss['Grec1p'])    
            g_loss += g_loss_rec1p   

          ############################## Attention Part ###################################
          if 'Attention' in GAN_options:
            g_loss_mask = self.config.lambda_mask * (torch.mean(rec_x1[1]) + torch.mean(fake_x1[1]))
            g_loss_mask_smooth = self.config.lambda_mask_smooth * (_compute_loss_smooth(rec_x1[1]) + _compute_loss_smooth(fake_x1[1])) 
            self.loss['Gatm'] = get_loss_value(g_loss_mask)
            self.loss['Gats'] = get_loss_value(g_loss_mask_smooth)     
            self.update_loss('Gatm', self.loss['Gatm'])
            self.update_loss('Gats', self.loss['Gats']) 
            self.color(self.loss, 'Gatm', 'blue')
            g_loss += g_loss_mask + g_loss_mask_smooth  

          ############################## Content Part ###################################
          if 'content_loss' in GAN_options:
            g_loss_content = self.config.lambda_content * criterion_l1(rec_x1[-1], fake_x1[-1].detach())
            self.loss['Gcon'] = get_loss_value(g_loss_content)
            self.update_loss('Gcon', self.loss['Gcon'])       
            g_loss += g_loss_content      

          ############################## Stochastic Part ###################################
          if 'Stochastic' in GAN_options: 
            _style_fake1 = self.G.get_style(fake_x1[0])
            s_loss_style = (self.config.lambda_style) * self._criterion_style(_style_fake1, style_fake1)
            self.loss['Gsty'] = get_loss_value(s_loss_style)
            self.update_loss('Gsty', self.loss['Gsty'])
            # if self.loss['Gsty']>0.75 and e>6 and style_flag:
            #   send_mail(body='Gsty still in {}'.format(self.loss['Gsty']))
            #   style_flag = False
            g_loss += s_loss_style

            if 'rec_style' in GAN_options:
              _style_rec1 = self.G.get_style(rec_x1[0])
              s_loss_style_rec = (self.config.lambda_style) * self._criterion_style(_style_rec1, style_rec1.detach())
              self.loss['Gstyr'] = get_loss_value(s_loss_style_rec)
              self.update_loss('Gstyr', self.loss['Gstyr'])
              g_loss += s_loss_style_rec              

              if 'content_loss' in GAN_options:
                rec_content = self.G(rec_x1[0], real_c1, JUST_CONTENT=True)
                g_loss_rec_content = self.config.lambda_content * criterion_l1(rec_content, fake_x1[-1].detach())
                self.loss['Gconr'] = get_loss_value(g_loss_rec_content)
                self.update_loss('Gconr', self.loss['Gconr'])       
                g_loss += g_loss_rec_content                    

          self.reset_grad()
          g_loss.backward()
          self.g_optimizer.step()          

        #=======================================================================================#
        #========================================MISCELANEOUS===================================#
        #=======================================================================================#
        # PRINT log info
        if (i+1) % self.config.log_step == 0 or (i+1)==last_model_step or i+e==0:
          progress_bar.set_postfix(**self.loss)
          if (i+1)==last_model_step: progress_bar.set_postfix('')
          if self.config.use_tensorboard:
            for tag, value in self.loss.items():
              self.logger.scalar_summary(tag, value, e * last_model_step + i + 1)

        # Save current fake
        if (i+1) % self.config.sample_step == 0 or (i+1)==last_model_step or i+e==0:
          name = os.path.join(self.config.sample_path, 'current_fake.jpg')
          self.save_fake_output(fixed_x, name, training=True)

      # Translate fixed images for debugging
      name = os.path.join(self.config.sample_path, '{}_{}_fake.jpg'.format(E, i+1))
      self.save_fake_output(fixed_x, name, training=True)

      self.save(E, i+1)
                 
      #Stats per epoch
      elapsed = time.time() - start_time
      elapsed = str(datetime.timedelta(seconds=elapsed))
      log = '--> %s | Elapsed (%d/%d) : %s | %s\nTrain'%(TimeNow(), e, self.config.num_epochs, elapsed, Log)
      for tag, value in sorted(self.LOSS.items()):
        log += ", {}: {:.4f}".format(tag, np.array(value).mean())   

      self.PRINT(log)
      self.data_loader.dataset.shuffle(e) #Shuffling dataset after each epoch

      # Decay learning rate     
      if (e+1) % self.config.num_epochs_decay ==0:
        g_lr = (g_lr / 10)
        d_lr = (d_lr / 10)
        self.update_lr(g_lr, d_lr)
        self.PRINT ('Decay learning rate to g_lr: {}, d_lr: {}.'.format(g_lr, d_lr))

  #=======================================================================================#
  #=======================================================================================#

  def save_fake_output(self, real_x, save_path, gif=False, output=False, training=False, Style=0):
    self.G.eval()  
    self.D.eval()
    Attention = 'Attention' in self.config.GAN_options
    Stochastic = 'Stochastic' in self.config.GAN_options
    n_rep = self.config.style_debug
    Output = []
    opt = torch.no_grad() if int(torch.__version__.split('.')[1])>3 else open('_null.txt', 'w')
    with opt:
      real_x = to_var(real_x, volatile=True)
      target_c_list = target_debug_list(real_x.size(0), self.config.c_dim)   

      # Start translations
      fake_image_list = [real_x]
      fake_attn_list  = []
      if Attention: 
        fake_attn_list = [to_var(denorm(real_x.data), volatile=True)]

      out_label = 0
      if not self.config.NO_LABELCUM:
        out_label = self._CLS(real_x)

      # Batch of images
      if Style==0:
        for target_c in target_c_list:
          target_c = torch.clamp(target_c+out_label,max=1)
          if Stochastic: 
            style=to_var(self.G.random_style(real_x), volatile=True)
          else:
            style = None
          fake_x = self.G(real_x, target_c, stochastic=style)
          fake_image_list.append(fake_x[0])
          if Attention: fake_attn_list.append(fake_x[1].repeat(1,3,1,1))
        Output.extend(self._SAVE_IMAGE(save_path, fake_image_list, attn_list=fake_attn_list, gif=gif))

      #Same image different style
      if Stochastic:
        for idx, real_x0 in enumerate(real_x):
          if training:
            _save_path = save_path
          else:
            _save_path = os.path.join(save_path.replace('.jpg', ''), '{}_{}.jpg'.format(Style, str(idx).zfill(3)))
            create_dir(_save_path)
          real_x0 = real_x0.repeat(n_rep,1,1,1)#.unsqueeze(0)
          _out_label = out_label[idx].repeat(n_rep,1)
          fake_image_list = [real_x0]
          fake_attn_list  = []            
          for _target_c in target_c_list:
            _target_c  = _target_c[0].repeat(n_rep,1)
            target_c = torch.clamp(_target_c+_out_label, max=1)
            style=to_var(self.G.random_style(real_x0), volatile=True)

            if Style==1:
              for j, i in enumerate(range(real_x0.size(0))): 
                style[i] = style[0].clone()#*_target_c[0].unsqueeze(-1)
                target_c[i] = target_c[i]*(0.2*j)
            elif Style==2:
              for j, i in enumerate(range(real_x0.size(0))): 
                style[i] = style[i]*0
                target_c[i] = target_c[i]*(0.2*j)
            elif Style==3:
              for j, i in enumerate(range(real_x0.size(0))): 
                style[i] = style[i]*_target_c[0].unsqueeze(-1)
            elif Style==4:
              #Extract style from the two before, current, and two after. 
              _real_x0 = torch.zeros_like(real_x0)
              for j, k in enumerate(range(-int(self.config.style_debug//2), 1+int(self.config.style_debug//2))):
                kk = (k+idx)%real_x.size(0) if k+idx >= real_x.size(0) else k+idx
                _real_x0[j] = real_x[kk]
              style = self.G.get_style(_real_x0)

            fake_x = self.G(real_x0, target_c, stochastic=style)
            fake_image_list.append(fake_x[0])
            if Attention: fake_attn_list.append(fake_x[1].repeat(1,3,1,1))
          Output.extend(self._SAVE_IMAGE(_save_path, fake_image_list, attn_list=fake_attn_list, gif=gif, mode='style'))
          if idx==self.config.iter_style: break          
    self.G.train()
    self.D.train()
    if output: return Output 
   
  #=======================================================================================#
  #=======================================================================================#

  def test(self, dataset='', load=False):
    import re
    from data_loader import get_loader
    if dataset=='': dataset = 'BP4D'
    last_name = self.resume_name()
    data_loader_val = get_loader(self.config.metadata_path, self.config.image_size, self.config.batch_size, shuffling=True, dataset=dataset, mode='test') 
    for i, (real_x, org_c, files) in enumerate(data_loader_val):
      save_folder = os.path.join(self.config.sample_path, '{}_test'.format(last_name))
      create_dir(save_folder)
      save_path = os.path.join(save_folder, '{}_{}_{}.jpg'.format(dataset, i+1, '{}'))
      string = '{}'
      if self.config.NO_LABELCUM:
        string += '_{}'.format('NO_Label_Cum','{}')
      string = string.format(TimeNow_str())
      name = os.path.abspath(save_path.format(string))
      for k in range(self.config.style_label_debug):
        output = self.save_fake_output(real_x, name, output=True, Style=k)
        # send_mail(body='Images from '+self.config.sample_path, attach=output)
      self.PRINT('Translated test images and saved into "{}"..!'.format(name))
      if i==self.config.iter_test-1: break   

  #=======================================================================================#
  #=======================================================================================#

  def DEMO(self, path):
    from data_loader import get_loader
    import re
    last_name = self.resume_name()
    batch_size = self.config.batch_size if not 'Stochastic' in self.config.GAN_options else 1
    data_loader = get_loader(path, self.config.image_size, batch_size, shuffling = False, dataset='DEMO', mode='test')
    only_one = data_loader.dataset.len==1 or 'Stochastic' in self.config.GAN_options
    for real_x in data_loader:
      save_path = os.path.join(self.config.sample_path, '{}_fake_val_DEMO_{}.jpg'.format(last_name, TimeNow_str()))
      output = self.save_fake_output(real_x, save_path, output=True, gif=True, only_one=only_one, idx_style='Stochastic' in self.config.GAN_options)
      send_mail(body='Images from '+self.config.sample_path, attach=output)
      self.PRINT('Translated test images and saved into "{}"..!'.format(save_path))