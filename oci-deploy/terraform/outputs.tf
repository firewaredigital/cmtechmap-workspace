# ==============================================================================
# CM TECHMAP — OCI Always Free — Terraform Outputs
# ==============================================================================

output "instance_id" {
  description = "OCID da instância de computação"
  value       = oci_core_instance.cm_techmap.id
}

output "instance_public_ip" {
  description = "Endereço IP público da instância"
  value       = oci_core_instance.cm_techmap.public_ip
}

output "instance_private_ip" {
  description = "Endereço IP privado da instância"
  value       = oci_core_instance.cm_techmap.private_ip
}

output "vcn_id" {
  description = "OCID da VCN criada"
  value       = oci_core_vcn.cm_techmap.id
}

output "subnet_id" {
  description = "OCID da sub-rede pública"
  value       = oci_core_subnet.cm_techmap_public.id
}

output "block_volume_id" {
  description = "OCID do block volume de dados"
  value       = oci_core_volume.cm_techmap_data.id
}

output "ssh_command" {
  description = "Comando SSH para acessar a instância"
  value       = "ssh -i ${var.ssh_private_key_path} ubuntu@${oci_core_instance.cm_techmap.public_ip}"
}

output "app_url_ip" {
  description = "URL da aplicação via IP"
  value       = "http://${oci_core_instance.cm_techmap.public_ip}"
}

output "app_url_https" {
  description = "URL HTTPS da aplicação (após configurar domínio e SSL)"
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "https://${oci_core_instance.cm_techmap.public_ip} (self-signed)"
}

output "api_docs_url" {
  description = "URL da documentação da API"
  value       = var.domain_name != "" ? "https://${var.domain_name}/docs" : "http://${oci_core_instance.cm_techmap.public_ip}/docs"
}

output "deployment_summary" {
  description = "Resumo do deploy"
  value       = <<-EOT

    ═══════════════════════════════════════════════════════════════════
    ✅ CM TECHMAP — OCI Always Free — Infraestrutura Provisionada
    ═══════════════════════════════════════════════════════════════════

    🖥️  Instância: ${var.instance_display_name}
    🏗️  Shape:     ${var.instance_shape} (${var.instance_ocpus} OCPUs, ${var.instance_memory_gb} GB RAM)
    🌐 IP:        ${oci_core_instance.cm_techmap.public_ip}
    💾 Storage:   50 GB boot + ${var.block_volume_size_gb} GB block volume
    🌍 Região:    ${var.region}

    📡 SSH:       ssh -i ${var.ssh_private_key_path} ubuntu@${oci_core_instance.cm_techmap.public_ip}

    Próximo passo:
      ./oci-deploy/scripts/deploy-oci.sh

    ═══════════════════════════════════════════════════════════════════
  EOT
}
