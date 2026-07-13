#!/bin/bash
# ==============================================================================
# CM TECHMAP — SSL Certificate Generation
# Generates self-signed certs if no Let's Encrypt certs exist
# ==============================================================================

SSL_DIR="/etc/nginx/ssl"
DOMAIN="${DOMAIN_NAME:-localhost}"

# Check if certificates already exist (Let's Encrypt or mounted)
if [ -f "$SSL_DIR/fullchain.pem" ] && [ -f "$SSL_DIR/privkey.pem" ]; then
    echo "[SSL] Certificates found — using existing certificates"
    exit 0
fi

echo "[SSL] No certificates found — generating self-signed for: $DOMAIN"

# Generate self-signed certificate (valid 365 days)
openssl req -x509 -nodes -days 365 \
    -newkey rsa:2048 \
    -keyout "$SSL_DIR/privkey.pem" \
    -out "$SSL_DIR/fullchain.pem" \
    -subj "/C=BR/ST=Goias/L=Goiania/O=CM TechMap/OU=Platform/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN,DNS:www.$DOMAIN,DNS:localhost,IP:127.0.0.1"

# Create chain.pem (same as fullchain for self-signed)
cp "$SSL_DIR/fullchain.pem" "$SSL_DIR/chain.pem"

echo "[SSL] Self-signed certificate generated for $DOMAIN"
echo "[SSL] ⚠️  For production, replace with Let's Encrypt certificates:"
echo "[SSL]    certbot certonly --webroot -w /var/www/certbot -d $DOMAIN -d www.$DOMAIN"
