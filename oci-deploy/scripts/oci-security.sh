#!/bin/bash
# ==============================================================================
# CM TECHMAP — OCI Security Hardening
# Executa DENTRO da VM para hardening adicional pós-deploy
# ==============================================================================

set -euo pipefail

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Security Hardening                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# ── 1. SSH Hardening ─────────────────────────────────────────────────────────
echo "🔐 [1/5] SSH Hardening..."

SSHD_CONFIG="/etc/ssh/sshd_config"
cp "$SSHD_CONFIG" "${SSHD_CONFIG}.bak"

# Disable password auth, root login
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD_CONFIG"
sed -i 's/^#*UsePAM.*/UsePAM no/' "$SSHD_CONFIG"
sed -i 's/^#*X11Forwarding.*/X11Forwarding no/' "$SSHD_CONFIG"

# Add rate limiting
if ! grep -q "MaxAuthTries" "$SSHD_CONFIG"; then
    echo "MaxAuthTries 3" >> "$SSHD_CONFIG"
    echo "MaxSessions 5" >> "$SSHD_CONFIG"
    echo "LoginGraceTime 30" >> "$SSHD_CONFIG"
fi

systemctl restart sshd
echo "  ✅ SSH hardened (root disabled, password disabled)"

# ── 2. fail2ban ──────────────────────────────────────────────────────────────
echo "🛡️  [2/5] fail2ban..."

apt-get install -y fail2ban >/dev/null 2>&1 || true

cat > /etc/fail2ban/jail.local << 'F2B'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
backend = systemd

[sshd]
enabled = true
port = ssh
filter = sshd
maxretry = 3
bantime = 7200
F2B

systemctl enable fail2ban
systemctl restart fail2ban
echo "  ✅ fail2ban ativo (SSH: 3 tentativas → ban 2h)"

# ── 3. UFW Firewall ─────────────────────────────────────────────────────────
echo "🧱 [3/5] UFW Firewall..."

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable
echo "  ✅ UFW ativo (22, 80, 443 apenas)"

# ── 4. Automatic security updates ───────────────────────────────────────────
echo "🔄 [4/5] Automatic security updates..."

apt-get install -y unattended-upgrades >/dev/null 2>&1 || true

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'AUTO'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
AUTO

systemctl enable unattended-upgrades
echo "  ✅ Atualizações de segurança automáticas"

# ── 5. sysctl hardening ─────────────────────────────────────────────────────
echo "⚙️  [5/5] Kernel hardening..."

cat > /etc/sysctl.d/99-security.conf << 'KERN'
# Disable IP source routing
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
# Disable ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
# Enable SYN flood protection
net.ipv4.tcp_syncookies = 1
# Log martian packets
net.ipv4.conf.all.log_martians = 1
# Disable IPv6 (if not needed)
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
KERN

sysctl -p /etc/sysctl.d/99-security.conf >/dev/null 2>&1
echo "  ✅ Kernel hardening aplicado"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "✅ Security hardening concluído!"
echo "═══════════════════════════════════════════════════════════════"
