import torch
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
import torch.utils.data
import torchvision
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from pathlib import Path

batch_size = 64
workers = 1

# Images are 256 by 240 pixels. Resize them to 224 by 224; must be divisible by 16
image_size = 224  # Resized 2D image input
patch_size = 16  # Dimension of a patch
num_patches = (image_size // patch_size) ** 2  # Number of patches in total
num_channels = 3  # 3 channels for RGB
embed_dim = 768  # Hidden size D of ViT-Base model from paper, equal to [(patch_size ** 2) * num_channels]
num_heads = 12  # Number of self attention blocks
num_layers = 12  # Number of Transformer encoder layers
mlp_size = 3072  # Number of hidden units between each linear layer
dropout_size = 0.1
num_classes = 2  # Number of different classes to classify (i.e. AD and NC)
num_epochs = 7

# Create the dataset
working_directory = Path.cwd()
train_dataroot = "AD_NC/train"
test_dataroot = "AD_NC/test"
train_dataset = dset.ImageFolder(root=train_dataroot,
                            transform=transforms.Compose([
                            transforms.Resize((image_size, image_size)),
                            transforms.ToTensor(),
                        ]))

test_dataset = dset.ImageFolder(root=test_dataroot,
                            transform=transforms.Compose([
                            transforms.Resize((image_size, image_size)),
                            transforms.ToTensor(),
                        ]))

train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size,
                                        shuffle=True, num_workers=workers)

test_dataloader = torch.utils.data.DataLoader(test_dataset, batch_size=batch_size,
                                        shuffle=False, num_workers=workers)



device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
if not torch.cuda.is_available():
    print("Warning CUDA not found. Using CPU")

# ------------------------------------------------------------------
# Patch Embedding
class PatchEmbedding(nn.Module):
    """Takes a 2D input image and splits it into fixed-sized patches and linearly embeds each of them.

    Changes the dimensions from H x W x C to N x (P^2 * C), where 
    (H, W, C) is the height, width, number of channels of the image,
    N is the number of patches, 
    and P is the dimension of each patch; P^2 represents a flattened patch.
    """
    def __init__(self, ngpu):
        super(PatchEmbedding, self).__init__()
        self.ngpu = ngpu
        # Puts image through Conv2D layer with kernel_size = stride to ensure no patches overlap.
        # This will split image into fixed-sized patches; each patch has the same dimensions
        # Then, each patch is flattened, including all channels for each patch.
        self.main = nn.Sequential(
            nn.Conv2d(in_channels=num_channels,
                        out_channels=embed_dim,
                        kernel_size=patch_size,
                        stride=patch_size,
                        padding=0),
            nn.Flatten(start_dim=2, end_dim=3)
        )

    def forward(self, input):
        return self.main(input).permute(0, 2, 1)  # Reorder the dimensions

# ------------------------------------------------------------------
# Transformer Encoder
class TransformerEncoder(nn.Module):
    """Creates a standard Transformer Encoder.
    One transformer encoder layer consists of layer normalisation, multi-head self attention layer, 
    a residual connection, another layer normalisation, an mlp block, and another residual connection.
    """
    def __init__(self, ngpu):
        super(TransformerEncoder, self).__init__()
        self.ngpu = ngpu

        self.transformer_encoder_layer = nn.TransformerEncoderLayer(d_model=embed_dim,
                                                                    nhead=num_heads,
                                                                    dim_feedforward=mlp_size,
                                                                    dropout=dropout_size,
                                                                    activation="gelu",
                                                                    layer_norm_eps=1e-5,
                                                                    batch_first=True,
                                                                    norm_first=True
                                                                    )
        
        self.full_transformer_encoder = nn.TransformerEncoder(encoder_layer=self.transformer_encoder_layer,
                                                                num_layers=num_layers)
    
    def forward(self, input):
        return self.full_transformer_encoder(input)

# ------------------------------------------------------------------
# MLP head
class MLPHead(nn.Module):
    """Creates an MLP head.
    Consists of a layer normalisation and a linear layer.
    """
    def __init__(self, ngpu):
        super(MLPHead, self).__init__()
        self.ngpu = ngpu

        self.main = nn.Sequential(nn.LayerNorm(normalized_shape=embed_dim),
                                    nn.Linear(in_features=embed_dim,
                                                out_features=num_classes)
                                    )
    
    def forward(self, input):
        return self.main(input)

# ------------------------------------------------------------------
# Multi-head Attnetion
class MultiheadSelfAttention(nn.Module):
    """Creates a multi-head self attention block.
    For an input sequence z, which contains all the feature maps, computes a weighted sum for all values in z.
    These attention weights between 2 elements are based on how similar their query representation and key 
    representation are.
    This is calculated by softmax((key_m * query_n) / sqrt(dim(key_m)))*V_m

    This block uses a Multi-head self attention structure, which contains multiple different projection layers
    of self attention.
    """
    def __init__(self, ngpu):
        super(MultiheadSelfAttention, self).__init__()
        self.ngpu = ngpu

        self.norm = nn.LayerNorm(normalized_shape=embed_dim)
        self.msa = nn.MultiheadAttention(embed_dim=embed_dim,
                                        num_heads=num_heads,
                                        batch_first=True)
        
    def forward(self, input):
        input = self.norm(input)
        attention, _ = self.msa(query=input,
                            key=input,
                            value=input,
                            need_weights=False)
        return attention
    

class ViT(nn.Module):
    """Creates a vision transformer model."""
    def __init__(self, ngpu):
        super(ViT, self).__init__()
        self.ngpu = ngpu

        self.patch_embedding = PatchEmbedding(workers)
        self.prepend_embed_token = nn.Parameter(torch.randn(1, 1, embed_dim), requires_grad=True)
        self.position_embed_token = nn.Parameter(torch.randn(1, num_patches + 1, embed_dim), requires_grad=True)
        self.embedding_dropout = nn.Dropout(p=dropout_size)  # Apply dropout after positional embedding as well
        self.transformer_encoder = TransformerEncoder(workers)
        self.mlp_head = MLPHead(workers)

    def forward(self, input):
        current_batch_size = input.size(0)
        prepend_embed_token_expanded = self.prepend_embed_token.expand(current_batch_size, -1, -1)
        input = self.patch_embedding(input)  # Patch embedding
        input = torch.cat((prepend_embed_token_expanded, input), dim=1)  # Prepend class token
        input = input + self.position_embed_token  # Add position embedding
        input = self.embedding_dropout(input)  # Apply dropout
        input = self.transformer_encoder(input)  # Feed into transformer encoder layers
        input = self.mlp_head(input[:, 0])  # Get final classificaiton from MLP head
        return input


def main():
    visual_transformer = ViT(workers).to(device)
    
    # ----------------------------------------
    # Loss Function and Optimiser
    criterion = nn.CrossEntropyLoss()
    optimiser = optim.Adam(params=visual_transformer.parameters(), lr=3e-3, weight_decay=0.3)

    # ----------------------------------------
    # Training loop
    visual_transformer.train()
    import time
    start_time = time.time()
    print("Starting training loop")
    
    for epoch in range(num_epochs):
        running_loss = 0.0
        for index, (inputs, labels) in enumerate(train_loader):
            inputs, labels = inputs.to(device), labels.to(device)
            
            optimiser.zero_grad()
            outputs = visual_transformer(inputs)
            loss = criterion(outputs, labels)

            loss.backward()
            optimiser.step()

            running_loss += loss.item()
            if (index+1) % 50 == 0:
                running_time = time.time()
                print("Epoch [{}/{}], Loss: {:.5f}".format(epoch+1, num_epochs, loss.item()))
                print(f"Timer: {running_time - start_time}")
                running_loss = 0.0

    print(f"Finished Training")

    # ----------------------------------------
    # Testing loop
    print("Testing...")
    start = time.time()
    visual_transformer.eval()
    with torch.no_grad():
        correct = 0
        total = 0
        for images, labels in test_dataloader:
            images = images.to(device)
            labels = labels.to(device)
            outputs = visual_transformer(images)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
        
        print('Test Accuracy: {} %'.format(100 * correct / total))
    end = time.time()
    print(f"Testing took: {end - start}")

if __name__ == '__main__':
    main()