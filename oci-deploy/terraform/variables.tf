# ==============================================================================
# CM TECHMAP — OCI Always Free — Terraform Variables
# ==============================================================================

# ── OCI Authentication ───────────────────────────────────────────────────────
variable "tenancy_ocid" {
  description = "OCID da tenancy Oracle Cloud"
  type        = string
}

variable "user_ocid" {
  description = "OCID do usuário OCI (para autenticação API Key)"
  type        = string
  default     = ""
}

variable "fingerprint" {
  description = "Fingerprint da API Key do usuário"
  type        = string
  default     = ""
}

variable "private_key_path" {
  description = "Caminho para a chave privada da API Key (.pem)"
  type        = string
  default     = "~/.oci/oci_api_key.pem"
}

variable "region" {
  description = "Região OCI (idealmente próxima ao Brasil)"
  type        = string
  default     = "sa-saopaulo-1"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "Região deve estar no formato: xx-nome-N (ex: sa-saopaulo-1)"
  }
}

variable "compartment_ocid" {
  description = "OCID do compartment (use tenancy_ocid para root compartment)"
  type        = string
}

# ── SSH Access ───────────────────────────────────────────────────────────────
variable "ssh_public_key" {
  description = "Chave pública SSH para acesso à VM (conteúdo, não caminho)"
  type        = string
  sensitive   = true
}

variable "ssh_private_key_path" {
  description = "Caminho para a chave privada SSH (para provisioning)"
  type        = string
  default     = "~/.ssh/id_rsa"
}

# ── Compute Instance ────────────────────────────────────────────────────────
variable "instance_display_name" {
  description = "Nome da instância de computação"
  type        = string
  default     = "cm-techmap-server"
}

variable "instance_shape" {
  description = "Shape da instância (Always Free ARM)"
  type        = string
  default     = "VM.Standard.A1.Flex"
}

variable "instance_ocpus" {
  description = "Número de OCPUs (max 4 para Always Free)"
  type        = number
  default     = 4

  validation {
    condition     = var.instance_ocpus >= 1 && var.instance_ocpus <= 4
    error_message = "Always Free permite no máximo 4 OCPUs para A1.Flex"
  }
}

variable "instance_memory_gb" {
  description = "Memória RAM em GB (max 24 para Always Free)"
  type        = number
  default     = 24

  validation {
    condition     = var.instance_memory_gb >= 1 && var.instance_memory_gb <= 24
    error_message = "Always Free permite no máximo 24 GB para A1.Flex"
  }
}

variable "os_image_id" {
  description = "OCID da imagem do OS (Ubuntu 22.04 Minimal aarch64). Se vazio, busca automaticamente."
  type        = string
  default     = ""
}

# ── Block Volume ─────────────────────────────────────────────────────────────
variable "block_volume_size_gb" {
  description = "Tamanho do block volume para dados em GB"
  type        = number
  default     = 150

  validation {
    condition     = var.block_volume_size_gb >= 50 && var.block_volume_size_gb <= 150
    error_message = "Boot (50 GB) + Block Volume devem caber nos 200 GB Always Free"
  }
}

# ── Networking ───────────────────────────────────────────────────────────────
variable "vcn_cidr_block" {
  description = "CIDR block da VCN"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidr_block" {
  description = "CIDR block da sub-rede pública"
  type        = string
  default     = "10.0.1.0/24"
}

# ── Application ──────────────────────────────────────────────────────────────
variable "domain_name" {
  description = "Domínio da aplicação (opcional, pode usar IP direto)"
  type        = string
  default     = ""
}

variable "app_name" {
  description = "Nome da aplicação para labels e tags"
  type        = string
  default     = "cm-techmap"
}

# ── Tags ─────────────────────────────────────────────────────────────────────
variable "freeform_tags" {
  description = "Tags livres para os recursos OCI"
  type        = map(string)
  default = {
    "project"     = "cm-techmap"
    "environment" = "staging"
    "tier"        = "always-free"
    "managed-by"  = "terraform"
  }
}
