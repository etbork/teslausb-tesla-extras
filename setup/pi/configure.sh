#!/bin/bash -eu

REPO=${REPO:-cimryan}
REPOSITORY=${REPOSITORY:-teslausb}
BRANCH=${BRANCH:-master}

PORTAL_ENABLED=${PORTAL_ENABLED:-false}

export INSTALL_DIR=${INSTALL_DIR:-/root/bin}

function get_script () {
    local local_path="$1"
    local name="$2"
    local remote_path="${3:-}"
    local ref="refs/heads/$BRANCH"

    echo "Starting download for $local_path/$name"
    curl --fail --show-error --location -o "$local_path/$name" https://raw.githubusercontent.com/"$REPO"/"$REPOSITORY"/"$ref"/"$remote_path"/"$name"
    chmod +x "$local_path/$name"
    echo "Done"
}

function install_portal_scripts () {
    local install_path="$1"

    echo "Installing portal scripts into $install_path"
    get_script "$install_path" remountfs_rw run
    get_script "$install_path" portal-session run
    get_script "$install_path" teslausb-portal.py run
    install -m 755 "$install_path/portal-session" /usr/local/bin/teslausb-portal-session
    install -m 755 "$install_path/teslausb-portal.py" /usr/local/bin/teslausb-portal
}

function install_portal_packages () {
    echo "Installing portal packages..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get --assume-yes install python3 hostapd dnsmasq
    systemctl unmask hostapd || true
}

function configure_portal_hotspot () {
    local serial="$(awk -F ': ' '/^Serial/ {print $2}' /proc/cpuinfo 2>/dev/null | tail -n 1)"
    local serial_last8="${serial: -8}"
    if [ "${#serial_last8}" -lt 8 ]
    then
        serial_last8="00000000"
    fi
    local ssid="${PORTAL_WIFI_SSID:-Glovebox-$serial}"
    local password="${PORTAL_WIFI_PASSWORD:-$serial_last8}"
    local address="${PORTAL_ADDRESS:-192.168.50.1}"
    local dhcp_start="${PORTAL_DHCP_START:-192.168.50.20}"
    local dhcp_end="${PORTAL_DHCP_END:-192.168.50.80}"
    local channel="${PORTAL_WIFI_CHANNEL:-6}"

    if [ "${#password}" -lt 8 ]
    then
        echo "STOP: PORTAL_WIFI_PASSWORD must be at least 8 characters for WPA2."
        exit 1
    fi

    echo "Configuring TeslaUSB portal hotspot $ssid at $address..."

    cat > /etc/hostapd/hostapd.conf <<EOF
country_code=${PORTAL_WIFI_COUNTRY:-US}
interface=wlan0
driver=nl80211
ssid=$ssid
hw_mode=g
channel=$channel
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$password
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

    cat > /etc/dnsmasq.d/teslausb-portal.conf <<EOF
interface=wlan0
bind-interfaces
domain-needed
bogus-priv
dhcp-range=$dhcp_start,$dhcp_end,255.255.255.0,24h
address=/teslausb.local/$address
address=/#/$address
EOF

    cat > /etc/systemd/system/teslausb-portal-network.service <<EOF
[Unit]
Description=TeslaUSB portal hotspot network
Before=hostapd.service dnsmasq.service
Wants=hostapd.service dnsmasq.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip link set wlan0 up
ExecStart=/sbin/ip addr flush dev wlan0
ExecStart=/sbin/ip addr add $address/24 dev wlan0

[Install]
WantedBy=multi-user.target
EOF

    sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd || true

    systemctl daemon-reload
    systemctl enable teslausb-portal-network.service
    systemctl enable hostapd
    systemctl enable dnsmasq
}

function configure_portal_service () {
    local uploads_enabled="${PORTAL_UPLOADS_ENABLED:-true}"
    local port="${PORTAL_PORT:-80}"

    cat > /etc/systemd/system/teslausb-portal.service <<EOF
[Unit]
Description=TeslaUSB web portal
After=network.target teslausb-portal-network.service

[Service]
Type=simple
Environment=PORTAL_UPLOADS_ENABLED=$uploads_enabled
Environment=PORTAL_PORT=$port
ExecStart=/usr/local/bin/teslausb-portal
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable teslausb-portal.service
}

function configure_portal () {
    if [ "$PORTAL_ENABLED" != "true" ]
    then
        echo "Portal mode not configured."
        return
    fi

    install_portal_packages
    install_portal_scripts "$INSTALL_DIR"
    configure_portal_hotspot
    configure_portal_service
}

if ! [ "$(id -u)" = 0 ]
then
    echo "STOP: Run sudo -i."
    exit 1
fi

if [ ! -e "$INSTALL_DIR" ]
then
    mkdir "$INSTALL_DIR"
fi

echo "Getting files from $REPO:$BRANCH"

configure_portal
