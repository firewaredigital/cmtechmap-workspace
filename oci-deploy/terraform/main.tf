# ==============================================================================
# CM TECHMAP — OCI Always Free — Terraform Main
# Provisiona toda a infraestrutura necessária no Oracle Cloud Always Free
# ==============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
}

# ── Provider OCI ─────────────────────────────────────────────────────────────
provider "oci" {
  tenancy_ocid     = var.tenancy_ocid
  user_ocid        = var.user_ocid
  fingerprint      = var.fingerprint
  private_key_path = var.private_key_path
  region           = var.region
}

# ── Data Sources ─────────────────────────────────────────────────────────────

# Availability Domain (usa o primeiro disponível)
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# Ubuntu 22.04 Minimal aarch64 image (busca automaticamente a mais recente)
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04 Minimal aarch64"
  shape                    = var.instance_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"

  filter {
    name   = "display_name"
    values = ["^Canonical-Ubuntu-22.04-Minimal-aarch64-.*"]
    regex  = true
  }
}

locals {
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  image_id            = var.os_image_id != "" ? var.os_image_id : data.oci_core_images.ubuntu_arm.images[0].id
  cloud_init_content  = file("${path.module}/cloud-init.yaml")
}

# ══════════════════════════════════════════════════════════════════════════════
# NETWORKING — VCN, Subnet, Internet Gateway, Route Table, Security List
# ══════════════════════════════════════════════════════════════════════════════

# ── VCN ──────────────────────────────────────────────────────────────────────
resource "oci_core_vcn" "cm_techmap" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = [var.vcn_cidr_block]
  display_name   = "${var.app_name}-vcn"
  dns_label      = "cmtechmap"
  freeform_tags  = var.freeform_tags
}

# ── Internet Gateway ────────────────────────────────────────────────────────
resource "oci_core_internet_gateway" "cm_techmap" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cm_techmap.id
  display_name   = "${var.app_name}-igw"
  enabled        = true
  freeform_tags  = var.freeform_tags
}

# ── Route Table ──────────────────────────────────────────────────────────────
resource "oci_core_route_table" "cm_techmap_public" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cm_techmap.id
  display_name   = "${var.app_name}-public-rt"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.cm_techmap.id
  }
}

# ── Security List (Firewall) ────────────────────────────────────────────────
resource "oci_core_security_list" "cm_techmap" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.cm_techmap.id
  display_name   = "${var.app_name}-security-list"
  freeform_tags  = var.freeform_tags

  # ── Egress: Permitir todo tráfego de saída ─────────────────────────────────
  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
    description = "Allow all outbound traffic"
  }

  # ── Ingress: SSH (porta 22) ────────────────────────────────────────────────
  ingress_security_rules {
    protocol    = "6" # TCP
    source      = "0.0.0.0/0"
    description = "SSH access"

    tcp_options {
      min = 22
      max = 22
    }
  }

  # ── Ingress: HTTP (porta 80) ───────────────────────────────────────────────
  ingress_security_rules {
    protocol    = "6" # TCP
    source      = "0.0.0.0/0"
    description = "HTTP access"

    tcp_options {
      min = 80
      max = 80
    }
  }

  # ── Ingress: HTTPS (porta 443) ─────────────────────────────────────────────
  ingress_security_rules {
    protocol    = "6" # TCP
    source      = "0.0.0.0/0"
    description = "HTTPS access"

    tcp_options {
      min = 443
      max = 443
    }
  }

  # ── Ingress: ICMP (ping) ───────────────────────────────────────────────────
  ingress_security_rules {
    protocol    = "1" # ICMP
    source      = "0.0.0.0/0"
    description = "ICMP (ping)"

    icmp_options {
      type = 3
      code = 4
    }
  }

  ingress_security_rules {
    protocol    = "1" # ICMP
    source      = var.vcn_cidr_block
    description = "ICMP from VCN"

    icmp_options {
      type = 3
    }
  }
}

# ── Sub-rede Pública ─────────────────────────────────────────────────────────
resource "oci_core_subnet" "cm_techmap_public" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.cm_techmap.id
  cidr_block                 = var.subnet_cidr_block
  display_name               = "${var.app_name}-public-subnet"
  dns_label                  = "cmtechpub"
  route_table_id             = oci_core_route_table.cm_techmap_public.id
  security_list_ids          = [oci_core_security_list.cm_techmap.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.freeform_tags
}

# ══════════════════════════════════════════════════════════════════════════════
# COMPUTE — VM ARM A1 Flex (Always Free)
# ══════════════════════════════════════════════════════════════════════════════

resource "oci_core_instance" "cm_techmap" {
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = var.instance_display_name
  shape               = var.instance_shape
  freeform_tags       = var.freeform_tags

  # ── Shape Configuration (ARM A1 Flex) ──────────────────────────────────────
  shape_config {
    ocpus         = var.instance_ocpus
    memory_in_gbs = var.instance_memory_gb
  }

  # ── Source (Ubuntu 22.04 ARM) ──────────────────────────────────────────────
  source_details {
    source_type             = "image"
    source_id               = local.image_id
    boot_volume_size_in_gbs = 50 # Always Free boot volume
  }

  # ── Network ────────────────────────────────────────────────────────────────
  create_vnic_details {
    subnet_id                 = oci_core_subnet.cm_techmap_public.id
    display_name              = "${var.app_name}-vnic"
    assign_public_ip          = true
    assign_private_dns_record = true
    hostname_label            = "cmtechmap"
  }

  # ── Cloud-Init (provisionamento automático) ────────────────────────────────
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data           = base64encode(local.cloud_init_content)
  }

  # Permite recriação de instâncias sem destruir volumes
  preserve_boot_volume = false

  lifecycle {
    # Ignora mudanças na imagem para evitar recreação acidental
    ignore_changes = [source_details[0].source_id]
  }
}

# ══════════════════════════════════════════════════════════════════════════════
# BLOCK VOLUME — 150 GB para dados persistentes
# ══════════════════════════════════════════════════════════════════════════════

resource "oci_core_volume" "cm_techmap_data" {
  compartment_id      = var.compartment_ocid
  availability_domain = local.availability_domain
  display_name        = "${var.app_name}-data-volume"
  size_in_gbs         = var.block_volume_size_gb
  freeform_tags       = var.freeform_tags

  # Usamos o VPU padrão (0-10, free tier usa balanced = 10)
  vpus_per_gb = 10
}

# ── Attach do Block Volume à VM ──────────────────────────────────────────────
resource "oci_core_volume_attachment" "cm_techmap_data" {
  attachment_type = "paravirtualized"
  instance_id     = oci_core_instance.cm_techmap.id
  volume_id       = oci_core_volume.cm_techmap_data.id
  display_name    = "${var.app_name}-data-attachment"

  # Não destruir o volume se a instância for recriada
  is_shareable = false
}
