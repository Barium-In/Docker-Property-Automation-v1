# Docker Property Automation

A comprehensive Docker Compose setup for deploying self-hosted services on Ubuntu servers. This repository is designed to be easily cloned and customized for different client deployments.

## 🚀 Quick Start

### Prerequisites

- Ubuntu Server (20.04 or later recommended)
- Docker and Docker Compose installed
- Git installed
- Root or sudo access

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/satvikknischal/Docker-Property-Automation-v1.git
   cd Docker-Property-Automation-v1
   ```

2. **Run the setup script (first time):**
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```
   The script will copy `.env.example` to `.env` and exit, prompting you to configure it.

3. **Configure the `.env` file:**
   ```bash
   nano .env  # or use your preferred editor
   ```
   - Set `BASE_DOMAIN` to your server's domain
   - Set `TZ`, `PUID`, `PGID`
   - Generate required secrets (use provided `openssl` commands in comments)
   - Configure storage paths for media, books, downloads, etc.
   - Add API keys for any LLM providers you use

4. **Run the setup script again to deploy:**
   ```bash
   ./setup.sh
   ```
   Confirm your `.env` is configured, then select which services to install from the interactive menu.

## 📁 Project Structure

```
.
├── .env.example              # Template configuration file
├── .env                      # Your actual configuration (not in git)
├── setup.sh                  # Interactive setup script
├── manage-all.sh             # Bulk service management
├── README.md
│
├── Nginx-Proxy-Manager/      # Reverse proxy & SSL
├── Cloudflared/              # Cloudflare Tunnel
├── Tailscale/                # Mesh VPN & remote access
├── Portainer/                # Docker management UI
├── NTP-Server/               # Network time server
│
├── Plex/                     # Media server
├── Frigate/                  # NVR for security cameras
├── Qubo-Bridge/              # Qubo camera P2P to local RTSP
├── Arr-Stack/                # Media automation
│   ├── Prowlarr/             # Indexer manager
│   ├── Radarr/               # Movies
│   ├── Sonarr/               # TV Shows
│   ├── Bazarr/               # Subtitles
│   ├── Seerr/                # Request management
│   └── Byparr/               # Captcha/Cloudflare bypass
├── qBittorrent/              # Torrent client
├── SabNZBd/                  # Usenet client
│
├── Immich/                   # Photo & video management
├── NextCloud-AIO/            # Cloud storage & collaboration
├── VaultWarden/              # Password manager
├── StirlingPDF/              # PDF tools
│
├── OpenWebUI/                # LLM chat interface
├── LiteLLM/                  # LLM API proxy
├── Vane/                     # AI search engine (formerly Perplexica)
├── SearXNG/                  # Privacy search
│
├── n8n/                      # Workflow automation
├── Homarr/                   # Dashboard
├── Uptime-Kuma/              # Status monitoring
│
├── Homebridge/               # HomeKit bridge
├── Whisper-STT/              # Speech-to-text
├── Piper-TTS/                # Text-to-speech
│
├── Tandoor/                  # Recipe manager
├── Book-Worms/               # Book management
│   ├── BookLore/             # Library manager
│   └── Shelfmark/            # Book downloader
│
├── Obsidian-LiveSync/        # Note sync server
└── Minecraft-Server/         # Game server
```

## 📦 Services Overview

### Infrastructure
| Service | Port | Description |
|---------|------|-------------|
| **Nginx Proxy Manager** | 80, 81, 443 | Reverse proxy with SSL management |
| **Portainer** | 9000 | Docker container management |
| **Cloudflared** | - | Cloudflare Tunnel for secure access |
| **Tailscale** | - | Mesh VPN, exit node & subnet router |
| **NTP Server** | 123/UDP | Network time synchronization |
| **Uptime Kuma** | 3001 | Service status monitoring |

### Media & Entertainment
| Service | Port | Description |
|---------|------|-------------|
| **Plex** | 32400 | Media streaming server |
| **Frigate** | 8971 | AI-powered NVR for cameras |
| **Qubo Bridge** | 8554 | Qubo camera as a local RTSP stream (not in `setup.sh`; see its README) |
| **Prowlarr** | 9696 | Indexer manager for *arr apps |
| **Radarr** | 7878 | Movie collection manager |
| **Sonarr** | 8989 | TV series collection manager |
| **Bazarr** | 6767 | Subtitle management for Radarr/Sonarr |
| **Seerr** | 5055 | Media request management |
| **Byparr** | 8191 | Captcha/Cloudflare bypass for indexers |
| **qBittorrent** | 8080 | Torrent download client |
| **SABnzbd** | 8080 | Usenet download client |

### AI & LLM
| Service | Port | Description |
|---------|------|-------------|
| **Open WebUI** | 8080 | Chat interface for LLMs |
| **LiteLLM** | 4000 | LLM API proxy (OpenAI, Claude, etc.) |
| **Vane** | 3000 | AI-powered search engine (formerly Perplexica) |
| **Whisper STT** | 10300 | Speech-to-text (Wyoming) |
| **Piper TTS** | 10200 | Text-to-speech (Wyoming) |

### Productivity
| Service | Port | Description |
|---------|------|-------------|
| **VaultWarden** | 80 | Bitwarden-compatible password manager |
| **NextCloud AIO** | 8080 | Cloud storage & collaboration |
| **n8n** | 5678 | Workflow automation |
| **StirlingPDF** | 8080 | PDF manipulation tools |
| **Homarr** | 7575 | Service dashboard |

### Home & Utility
| Service | Port | Description |
|---------|------|-------------|
| **Homebridge** | 8581 | HomeKit bridge (host network) |
| **Immich** | 2283 | Photo & video management |
| **Tandoor** | 80 | Recipe manager |
| **Obsidian LiveSync** | 5984 | Obsidian vault sync |

### Books
| Service | Port | Description |
|---------|------|-------------|
| **BookLore** | 6060 | Book library manager |
| **Shelfmark** | 8084 | Book download manager |

### Search
| Service | Port | Description |
|---------|------|-------------|
| **SearXNG** | 8080 | Privacy-focused meta search |

### Gaming
| Service | Port | Description |
|---------|------|-------------|
| **Minecraft** | 25565 | Minecraft game server |

## ⚙️ Configuration

### Root `.env` File

All services use a shared `.env` file in the project root. Key sections include:

1. **Server Configuration** - Domain, timezone, user IDs
2. **Container Versions** - Pinned versions for all images
3. **Network Configuration** - Docker network name
4. **Service-Specific Variables** - Credentials, paths, settings

### Version Management

All container images use version variables defined in `.env`:

```bash
# Pin versions for stability
PLEX_VERSION=1.43.0
IMMICH_VERSION=v2.5.2
LITELLM_VERSION=v1.81.0-stable

# Update: change version in .env, then redeploy
cd ServiceName
docker compose --env-file ../.env pull
docker compose --env-file ../.env up -d
```

## 🔧 Usage

### Managing Individual Services

```bash
cd ServiceName
docker compose --env-file ../.env up -d      # Start
docker compose --env-file ../.env down       # Stop
docker compose --env-file ../.env logs -f    # View logs
docker compose --env-file ../.env pull       # Update image
docker compose --env-file ../.env restart    # Restart
```

### Managing All Services

```bash
./manage-all.sh status    # Check all services
./manage-all.sh up        # Start all
./manage-all.sh down      # Stop all
./manage-all.sh pull      # Update all images
./manage-all.sh restart   # Restart all
```

## 🌐 Network Architecture

```
                    Internet
                        │
                        ▼
            ┌───────────────────┐
            │  Cloudflare (DNS) │
            └─────────┬─────────┘
                      │
            ┌─────────▼─────────┐
            │ Nginx Proxy Mgr   │ ◄── SSL Termination
            │   (ports 80/443)  │
            └─────────┬─────────┘
                      │
         ┌────────────┴────────────┐
         │      proxy-network      │
         │                         │
    ┌────┴────┐  ┌────┴────┐  ┌───┴────┐
    │ Service │  │ Service │  │Service │
    │    A    │  │    B    │  │   C    │
    └─────────┘  └─────────┘  └────────┘
```

### Special Network Modes

| Service | Network Mode | Reason |
|---------|--------------|--------|
| **Homebridge** | `host` | mDNS/Bonjour for HomeKit discovery |
| **Tailscale** | `host` | Subnet routing & exit node require host network access |
| **Minecraft** | Direct ports | Game clients need direct TCP access |
| **Plex** | Hybrid | Direct port for local network discovery |

## 🔒 Security Best Practices

1. **Never commit `.env` files** - They're in `.gitignore`
2. **Generate strong secrets** - Use provided `openssl` commands
3. **Use reverse proxy** - Route all HTTP traffic through NPM with SSL
4. **Limit port exposure** - Use `expose` instead of `ports` where possible
5. **Keep services updated** - Regularly run `docker compose pull`
6. **Backup credentials** - Store encryption keys securely

## 🐛 Troubleshooting

### Services can't communicate
```bash
# Verify network exists
docker network inspect proxy-network

# Check if service is on network
docker inspect <container_name> | grep -A 20 Networks
```

### Permission issues
```bash
# Check your user IDs
id $USER

# Update .env with correct values
PUID=1000
PGID=1000
```

### Container won't start
```bash
# Check logs
docker compose logs -f

# Verify environment variables
docker compose config
```

### Port conflicts
```bash
# Find what's using a port
sudo netstat -tulpn | grep :PORT
sudo lsof -i :PORT
```

## 📚 Service Documentation

| Service | Documentation |
|---------|---------------|
| Nginx Proxy Manager | https://nginxproxymanager.com/ |
| VaultWarden | https://github.com/dani-garcia/vaultwarden |
| Immich | https://immich.app/docs |
| Plex | https://support.plex.tv/ |
| Frigate | https://docs.frigate.video/ |
| *arr Apps | https://wiki.servarr.com/ |
| Bazarr | https://wiki.bazarr.media/ |
| Seerr | https://github.com/seerr-team/seerr |
| Byparr | https://github.com/ThePhaseless/Byparr |
| n8n | https://docs.n8n.io/ |
| LiteLLM | https://docs.litellm.ai/ |
| SearXNG | https://docs.searxng.org/ |
| Vane | https://github.com/ItzCrazyKns/Vane |
| Tandoor | https://docs.tandoor.dev/ |
| Homebridge | https://homebridge.io/ |
| Homarr | https://homarr.dev/docs/ |
| BookLore | https://github.com/adityachoudhary/booklore |
| Obsidian LiveSync | https://github.com/vrtmrz/obsidian-livesync |
| Tailscale | https://tailscale.com/kb/1282/docker |

## 🤝 Contributing

When adding or modifying services:

1. Follow the existing compose file patterns
2. Use `env_file: - ../.env` for root-level services
3. Add version variable to setup.sh
4. Add service to the `proxy-network`
5. Include healthcheck
6. Add compose labels
7. Document in this README


## 🆘 Support

For issues or questions, please [open an issue](your-repo-url/issues) or contact support@barium.in.
