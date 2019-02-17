import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models
import collections
import math

def weights_init(m):
    # Initialize filters with Gaussian random weights
    if isinstance(m, nn.Conv2d):
        n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
        m.weight.data.normal_(0, math.sqrt(2. / n))
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.ConvTranspose2d):
        n = m.kernel_size[0] * m.kernel_size[1] * m.in_channels
        m.weight.data.normal_(0, math.sqrt(2. / n))
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.BatchNorm2d):
        m.weight.data.fill_(1)
        m.bias.data.zero_()


class Unpool(nn.Module):
    # Unpool: 2*2 unpooling with zero padding
    def __init__(self, num_channels, stride=2):
        super(Unpool, self).__init__()

        self.num_channels = num_channels
        self.stride = stride

    def forward(self, x):
        weights = torch.zeros(self.num_channels, 1, self.stride, self.stride)
        if torch.cuda.is_available():
            weights = weights.cuda()
        weights[:, :, 0, 0] = 1
        return F.conv_transpose2d(x, weights, stride=self.stride, groups=self.num_channels)


class Decoder(nn.Module):
    names = ['deconv2', 'deconv3', 'upconv', 'upproj']

    def __init__(self):
        super(Decoder, self).__init__()

        self.layer1 = None
        self.layer2 = None
        self.layer3 = None
        self.layer4 = None

    def forward(self, x):
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        return x


class DeConv(Decoder):
    def __init__(self, in_channels, kernel_size):
        assert kernel_size >= 2, "kernel_size out of range: {}".format(kernel_size)
        super(DeConv, self).__init__()

        def convt(in_channels):
            stride = 2
            padding = (kernel_size - 1) // 2
            output_padding = kernel_size % 2
            assert -2 - 2 * padding + kernel_size + output_padding == 0, "deconv parameters incorrect"

            module_name = "deconv{}".format(kernel_size)
            return nn.Sequential(collections.OrderedDict([
                (module_name, nn.ConvTranspose2d(in_channels, in_channels // 2, kernel_size,
                                                 stride, padding, output_padding, bias=False)),
                ('batchnorm', nn.BatchNorm2d(in_channels // 2)),
                ('relu', nn.ReLU(inplace=True)),
            ]))

        self.layer1 = convt(in_channels)
        self.layer2 = convt(in_channels // 2)
        self.layer3 = convt(in_channels // (2 ** 2))
        self.layer4 = convt(in_channels // (2 ** 3))


class UpConv(Decoder):
    # UpConv decoder consists of 4 upconv modules with decreasing number of channels and increasing feature map size
    def upconv_module(self, in_channels):
        # UpConv module: unpool -> 5*5 conv -> batchnorm -> ReLU
        upconv = nn.Sequential(collections.OrderedDict([
            ('unpool', Unpool(in_channels)),
            ('conv', nn.Conv2d(in_channels, in_channels // 2, kernel_size=5, stride=1, padding=2, bias=False)),
            ('batchnorm', nn.BatchNorm2d(in_channels // 2)),
            ('relu', nn.ReLU()),
        ]))
        return upconv

    def __init__(self, in_channels):
        super(UpConv, self).__init__()
        self.layer1 = self.upconv_module(in_channels)
        self.layer2 = self.upconv_module(in_channels // 2)
        self.layer3 = self.upconv_module(in_channels // 4)
        self.layer4 = self.upconv_module(in_channels // 8)


class FasterUpConv(Decoder):
    # Faster Upconv using pixelshuffle

    class faster_upconv_module(nn.Module):

        def __init__(self, in_channel):
            super(FasterUpConv.faster_upconv_module, self).__init__()

            self.conv1_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=3)),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv2_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=(2, 3))),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv3_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=(3, 2))),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv4_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=2)),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.ps = nn.PixelShuffle(2)
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            # print('Upmodule x size = ', x.size())
            x1 = self.conv1_(nn.functional.pad(x, (1, 1, 1, 1)))
            x2 = self.conv2_(nn.functional.pad(x, (1, 1, 0, 1)))
            x3 = self.conv3_(nn.functional.pad(x, (0, 1, 1, 1)))
            x4 = self.conv4_(nn.functional.pad(x, (0, 1, 0, 1)))

            x = torch.cat((x1, x2, x3, x4), dim=1)

            output = self.ps(x)
            output = self.relu(output)

            return output

    def __init__(self, in_channel):
        super(FasterUpConv, self).__init__()

        self.layer1 = self.faster_upconv_module(in_channel)
        self.layer2 = self.faster_upconv_module(in_channel // 2)
        self.layer3 = self.faster_upconv_module(in_channel // 4)
        self.layer4 = self.faster_upconv_module(in_channel // 8)


class UpProj(Decoder):
    # UpProj decoder consists of 4 upproj modules with decreasing number of channels and increasing feature map size

    class UpProjModule(nn.Module):
        # UpProj module has two branches, with a Unpool at the start and a ReLu at the end
        #   upper branch: 5*5 conv -> batchnorm -> ReLU -> 3*3 conv -> batchnorm
        #   bottom branch: 5*5 conv -> batchnorm

        def __init__(self, in_channels):
            super(UpProj.UpProjModule, self).__init__()
            out_channels = in_channels // 2
            self.unpool = Unpool(in_channels)
            self.upper_branch = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channels, out_channels, kernel_size=5, stride=1, padding=2, bias=False)),
                ('batchnorm1', nn.BatchNorm2d(out_channels)),
                ('relu', nn.ReLU()),
                ('conv2', nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False)),
                ('batchnorm2', nn.BatchNorm2d(out_channels)),
            ]))
            self.bottom_branch = nn.Sequential(collections.OrderedDict([
                ('conv', nn.Conv2d(in_channels, out_channels, kernel_size=5, stride=1, padding=2, bias=False)),
                ('batchnorm', nn.BatchNorm2d(out_channels)),
            ]))
            self.relu = nn.ReLU()

        def forward(self, x):
            x = self.unpool(x)
            x1 = self.upper_branch(x)
            x2 = self.bottom_branch(x)
            x = x1 + x2
            x = self.relu(x)
            return x

    def __init__(self, in_channels):
        super(UpProj, self).__init__()
        self.layer1 = self.UpProjModule(in_channels)
        self.layer2 = self.UpProjModule(in_channels // 2)
        self.layer3 = self.UpProjModule(in_channels // 4)
        self.layer4 = self.UpProjModule(in_channels // 8)


class FasterUpProj(Decoder):
    # Faster UpProj decorder using pixelshuffle

    class faster_upconv(nn.Module):

        def __init__(self, in_channel):
            super(FasterUpProj.faster_upconv, self).__init__()

            self.conv1_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=3)),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv2_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=(2, 3))),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv3_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=(3, 2))),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.conv4_ = nn.Sequential(collections.OrderedDict([
                ('conv1', nn.Conv2d(in_channel, in_channel // 2, kernel_size=2)),
                ('bn1', nn.BatchNorm2d(in_channel // 2)),
            ]))

            self.ps = nn.PixelShuffle(2)
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            # print('Upmodule x size = ', x.size())
            x1 = self.conv1_(nn.functional.pad(x, (1, 1, 1, 1)))
            x2 = self.conv2_(nn.functional.pad(x, (1, 1, 0, 1)))
            x3 = self.conv3_(nn.functional.pad(x, (0, 1, 1, 1)))
            x4 = self.conv4_(nn.functional.pad(x, (0, 1, 0, 1)))
            # print(x1.size(), x2.size(), x3.size(), x4.size())

            x = torch.cat((x1, x2, x3, x4), dim=1)

            x = self.ps(x)
            return x

    class FasterUpProjModule(nn.Module):
        def __init__(self, in_channels):
            super(FasterUpProj.FasterUpProjModule, self).__init__()
            out_channels = in_channels // 2

            self.upper_branch = nn.Sequential(collections.OrderedDict([
                ('faster_upconv', FasterUpProj.faster_upconv(in_channels)),
                ('relu', nn.ReLU(inplace=True)),
                ('conv', nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False)),
                ('batchnorm', nn.BatchNorm2d(out_channels)),
            ]))
            self.bottom_branch = FasterUpProj.faster_upconv(in_channels)
            self.relu = nn.ReLU(inplace=True)

        def forward(self, x):
            x1 = self.upper_branch(x)
            x2 = self.bottom_branch(x)
            x = x1 + x2
            x = self.relu(x)
            return x

    def __init__(self, in_channel):
        super(FasterUpProj, self).__init__()

        self.layer1 = self.FasterUpProjModule(in_channel)
        self.layer2 = self.FasterUpProjModule(in_channel // 2)
        self.layer3 = self.FasterUpProjModule(in_channel // 4)
        self.layer4 = self.FasterUpProjModule(in_channel // 8)


def choose_decoder(decoder, in_channels):
    if decoder[:6] == 'deconv':
        assert len(decoder) == 7
        kernel_size = int(decoder[6])
        return DeConv(in_channels, kernel_size)
    elif decoder == "upproj":
        return UpProj(in_channels)
    elif decoder == "upconv":
        return UpConv(in_channels)
    elif decoder == "fasterupproj":
        return FasterUpProj(in_channels)
    else:
        assert False, "invalid option for decoder: {}".format(decoder)


class ResNet(nn.Module):
    def __init__(self, dataset = 'kitti', layers = 50, decoder = 'upproj', output_size=(228, 304), in_channels=3, pretrained=True):

        if layers not in [18, 34, 50, 101, 152]:
            raise RuntimeError(
                'Only 18, 34, 50, 101, and 152 layer model are defined for ResNet. Got {}'.format(layers))

        super(ResNet, self).__init__()
        pretrained_model = torchvision.models.__dict__['resnet{}'.format(layers)](pretrained=pretrained)

        if in_channels == 3:
            self.conv1 = pretrained_model._modules['conv1']
            self.bn1 = pretrained_model._modules['bn1']
        else:
            self.conv1 = nn.Conv2d(in_channels, 64, kernel_size=7, stride=2, padding=3, bias=False)
            self.bn1 = nn.BatchNorm2d(64)
            weights_init(self.conv1)
            weights_init(self.bn1)

        self.output_size = output_size

        self.relu = pretrained_model._modules['relu']
        self.maxpool = pretrained_model._modules['maxpool']
        self.layer1 = pretrained_model._modules['layer1']
        self.layer2 = pretrained_model._modules['layer2']
        self.layer3 = pretrained_model._modules['layer3']
        self.layer4 = pretrained_model._modules['layer4']

        # clear memory
        del pretrained_model

        # define number of intermediate channels
        if layers <= 34:
            num_channels = 512
        elif layers >= 50:
            num_channels = 2048

        self.conv2 = nn.Conv2d(num_channels, num_channels // 2, kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm2d(num_channels // 2)

        self.upSample = choose_decoder(decoder, num_channels // 2)

        # setting bias=true doesn't improve accuracy
        self.conv3 = nn.Conv2d(num_channels // 32, 1, kernel_size=3, stride=1, padding=1, bias=False)
        self.bilinear = nn.Upsample(size=self.output_size, mode='bilinear', align_corners=True)

        # weight init
        self.conv2.apply(weights_init)
        self.bn2.apply(weights_init)

        self.upSample.apply(weights_init)

        self.conv3.apply(weights_init)

    def forward(self, x):
        # resnet
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)
        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)

        x = self.conv2(x4)
        x = self.bn2(x)

        x = self.upSample(x)

        x = self.conv3(x)
        x = self.bilinear(x)

        return x

    def get_1x_lr_params(self):
        """
        This generator returns all the parameters of the net layer whose learning rate is 1x lr.
        """
        b = [self.conv1, self.bn1, self.relu, self.maxpool, self.layer1, self.layer2, self.layer3, self.layer4]
        for i in range(len(b)):
            for k in b[i].parameters():
                if k.requires_grad:
                    yield k

    def get_10x_lr_params(self):
        """
        This generator returns all the parameters of the net layer whose learning rate is 20x lr.
        """
        b = [self.conv2, self.bn2, self.upSample, self.conv3, self.bilinear]
        for j in range(len(b)):
            for k in b[j].parameters():
                if k.requires_grad:
                    yield k
import time
from data_loader import DataLoader
data = DataLoader('', '')

class MaskedMSELoss(nn.Module):
    def __init__(self):
        super(MaskedMSELoss, self).__init__()

    def forward(self, pred, target):
        assert pred.dim() == target.dim(), "inconsistent dimensions"
        valid_mask = (target > 0).detach()
        diff = target - pred
        diff = diff[valid_mask]
        self.loss = (diff ** 2).mean()
        return self.loss


class MaskedL1Loss(nn.Module):
    def __init__(self):
        super(MaskedL1Loss, self).__init__()

    def forward(self, pred, target):
        assert pred.dim() == target.dim(), "inconsistent dimensions"
        valid_mask = (target > 0).detach()
        diff = target - pred
        diff = diff[valid_mask]
        self.loss = diff.abs().mean()
        return self.loss


class berHuLoss(nn.Module):
    def __init__(self):
        super(berHuLoss, self).__init__()

    def forward(self, pred, target):
        assert pred.dim() == target.dim(), "inconsistent dimensions"

        huber_c = torch.max(pred - target)
        huber_c = 0.2 * huber_c

        valid_mask = (target > 0).detach()
        diff = target - pred
        diff = diff[valid_mask]
        diff = diff.abs()

        huber_mask = (diff > huber_c).detach()

        diff2 = diff[huber_mask]
        diff2 = diff2 ** 2

        self.loss = torch.cat((diff, diff2)).mean()

        return self.loss

print("=> creating Model")
model = ResNet(output_size=(90, 270))
print("=> model created.")
start_epoch = 0
lr = 0.001

train_params = [{'params': model.get_1x_lr_params(), 'lr': lr},
                {'params': model.get_10x_lr_params(), 'lr': lr * 10}]

optimizer = torch.optim.SGD(train_params, lr=lr, weight_decay=4e-5)

# You can use DataParallel() whether you use Multi-GPUs or not
model = nn.DataParallel(model).cuda()

# Define Loss Function
criterion = MaskedL1Loss()

def train(train_loader, model, criterion, optimizer, epoch):
    model.train()  # switch to train mode

    for iter_ in range(43000//32):
        # Adjust Learning Rate
        if iter_ % 100 == 0:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= 0.99
        
        input, target = next(train_loader.get_one_batch(32))
        input, target = input.cuda(), target.cuda()
        torch.cuda.synchronize()
        
        pred = model(input)

        loss = criterion(pred, target)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        torch.cuda.synchronize()
        
        if (iter_ + 1) % 10 == 0:
            print('Train Epoch: {0} Batch: [{1}/{2}]\t'
                  'Loss={Loss:.5f}'.format(
                epoch, iter_ + 1, 43000//32, 
                Loss=loss.item()))

for i in range(25):
    # Train the Model
    train(data, model, criterion, optimizer, i)
    
    # Save Checkpoint
    torch.save({
            'epoch': i,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'loss': loss,
            }, './ResNet.pth')

