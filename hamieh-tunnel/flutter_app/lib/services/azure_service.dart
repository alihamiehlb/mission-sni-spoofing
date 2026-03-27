import 'dart:convert';
import 'dart:math';
import 'package:http/http.dart' as http;
import 'api_service.dart';

/// Azure ARM returned a non-success status during provisioning.
class AzureProvisionException implements Exception {
  AzureProvisionException(this.message);
  final String message;
  @override
  String toString() => message;
}

class VmInfo {
  final String ip;
  final String status;
  final String token;
  final String subscriptionId;

  const VmInfo({
    required this.ip,
    required this.status,
    required this.token,
    required this.subscriptionId,
  });

  bool get isRunning => status == 'VM running';
  bool get isDeallocated =>
      status == 'VM deallocated' || status == 'VM stopped';
  bool get exists => status != 'not_found';

  Map<String, dynamic> toJson() => {
        'ip': ip,
        'status': status,
        'token': token,
        'subscriptionId': subscriptionId,
      };

  factory VmInfo.fromJson(Map<String, dynamic> j) => VmInfo(
        ip: j['ip'] ?? '',
        status: j['status'] ?? 'not_found',
        token: j['token'] ?? '',
        subscriptionId: j['subscriptionId'] ?? '',
      );

  factory VmInfo.notFound() => const VmInfo(
        ip: '',
        status: 'not_found',
        token: '',
        subscriptionId: '',
      );
}

class AzureService {
  static const _armBase = 'https://management.azure.com';
  static const _apiVersion = '2024-03-01';
  static const _netApiVersion = '2024-01-01';

  static const _rgName = 'rg-hashsec-tunnel';
  static const _vmName = 'vm-hashsec-relay';
  static const _nsgName = 'nsg-hashsec-relay';
  static const _location = 'swedencentral';
  static const _vmSize = 'Standard_B1s';
  static const _adminUser = 'hashsec';
  static const _relayPort = 9443;

  static const _vmInfoKey = 'hashsec_vm_info';

  String _accessToken;

  AzureService(this._accessToken);

  void updateToken(String token) => _accessToken = token;

  Map<String, String> get _headers => {
        'Authorization': 'Bearer $_accessToken',
        'Content-Type': 'application/json',
      };

  // -- Secure persisted VM info (encrypted via Android Keystore) --

  Future<void> _saveVmInfo(VmInfo info) async {
    await ApiService.secureWrite(_vmInfoKey, jsonEncode(info.toJson()));
  }

  Future<VmInfo> loadVmInfo() async {
    final raw = await ApiService.secureRead(_vmInfoKey);
    if (raw == null) return VmInfo.notFound();
    try {
      return VmInfo.fromJson(jsonDecode(raw));
    } catch (_) {
      return VmInfo.notFound();
    }
  }

  Future<void> clearVmInfo() async {
    await ApiService.secureDelete(_vmInfoKey);
  }

  String _generateToken(int length) {
    final rng = Random.secure();
    final bytes = List.generate(length, (_) => rng.nextInt(256));
    return base64Url.encode(bytes).replaceAll('=', '');
  }

  // -- Subscriptions --

  Future<List<Map<String, String>>> listSubscriptions() async {
    final resp = await http.get(
      Uri.parse('$_armBase/subscriptions?api-version=2022-12-01'),
      headers: _headers,
    );
    if (resp.statusCode != 200) return [];
    final data = jsonDecode(resp.body);
    final subs = (data['value'] as List?) ?? [];
    return subs
        .map<Map<String, String>>((s) => {
              'id': s['subscriptionId'] as String,
              'name': s['displayName'] as String,
            })
        .toList();
  }

  // -- Full VM provisioning --
  // Order: Resource Group -> NSG -> Public IP -> VNet -> NIC -> VM

  Future<VmInfo> createTunnel({
    required String subscriptionId,
    required void Function(String) onProgress,
  }) async {
    // Two independent random secrets
    final relayToken = _generateToken(32);
    final adminPassword = _generateToken(24);

    final sub = subscriptionId;
    final rgPath = '/subscriptions/$sub/resourceGroups/$_rgName';
    final nsgId =
        '$rgPath/providers/Microsoft.Network/networkSecurityGroups/$_nsgName';
    final pipId =
        '$rgPath/providers/Microsoft.Network/publicIPAddresses/$_vmName-pip';
    final vnetId =
        '$rgPath/providers/Microsoft.Network/virtualNetworks/$_vmName-vnet';
    final subnetId = '$vnetId/subnets/default';
    final nicId =
        '$rgPath/providers/Microsoft.Network/networkInterfaces/$_vmName-nic';
    final vmPath =
        '$rgPath/providers/Microsoft.Compute/virtualMachines/$_vmName';

    // 1. Resource Group
    onProgress('Creating resource group...');
    await _armPut(
      'Create resource group',
      '$rgPath?api-version=2024-03-01',
      body: {'location': _location},
    );

    // 2. NSG -- only relay port open, everything else denied by default
    onProgress('Setting up network security...');
    await _armPut(
      'Create network security group',
      '$nsgId?api-version=$_netApiVersion',
      body: {
        'location': _location,
        'properties': {
          'securityRules': [
            {
              'name': 'AllowRelay',
              'properties': {
                'priority': 1010,
                'direction': 'Inbound',
                'access': 'Allow',
                'protocol': 'Tcp',
                'sourceAddressPrefix': '*',
                'sourcePortRange': '*',
                'destinationAddressPrefix': '*',
                'destinationPortRange': '$_relayPort',
              },
            },
            {
              'name': 'DenySSH',
              'properties': {
                'priority': 1020,
                'direction': 'Inbound',
                'access': 'Deny',
                'protocol': 'Tcp',
                'sourceAddressPrefix': '*',
                'sourcePortRange': '*',
                'destinationAddressPrefix': '*',
                'destinationPortRange': '22',
              },
            },
            {
              'name': 'DenyAllOther',
              'properties': {
                'priority': 4000,
                'direction': 'Inbound',
                'access': 'Deny',
                'protocol': '*',
                'sourceAddressPrefix': '*',
                'sourcePortRange': '*',
                'destinationAddressPrefix': '*',
                'destinationPortRange': '*',
              },
            },
          ],
        },
      },
    );

    // 3. Static public IP
    onProgress('Allocating public IP...');
    await _armPut(
      'Create public IP',
      '$pipId?api-version=$_netApiVersion',
      body: {
        'location': _location,
        'sku': {'name': 'Standard'},
        'properties': {'publicIPAllocationMethod': 'Static'},
      },
    );
    await Future.delayed(const Duration(seconds: 5));

    // 4. Virtual network + subnet
    onProgress('Setting up virtual network...');
    await _armPut(
      'Create virtual network',
      '$vnetId?api-version=$_netApiVersion',
      body: {
        'location': _location,
        'properties': {
          'addressSpace': {
            'addressPrefixes': ['10.0.0.0/16'],
          },
          'subnets': [
            {
              'name': 'default',
              'properties': {
                'addressPrefix': '10.0.0.0/24',
                'networkSecurityGroup': {'id': nsgId},
              },
            },
          ],
        },
      },
    );
    await Future.delayed(const Duration(seconds: 5));

    // 5. NIC
    onProgress('Creating network interface...');
    await _armPut(
      'Create network interface',
      '$nicId?api-version=$_netApiVersion',
      body: {
        'location': _location,
        'properties': {
          'ipConfigurations': [
            {
              'name': 'ipconfig1',
              'properties': {
                'subnet': {'id': subnetId},
                'publicIPAddress': {'id': pipId},
              },
            },
          ],
          'networkSecurityGroup': {'id': nsgId},
        },
      },
    );
    await Future.delayed(const Duration(seconds: 10));

    // 6. VM with hardened cloud-init
    onProgress('Starting VM (takes ~90 seconds)...');
    final cloudInit = _buildCloudInit(relayToken);
    await _armPut(
      'Create virtual machine',
      '$vmPath?api-version=$_apiVersion',
      body: {
        'location': _location,
        'properties': {
          'hardwareProfile': {'vmSize': _vmSize},
          'osProfile': {
            'computerName': _vmName,
            'adminUsername': _adminUser,
            'adminPassword': 'Aa1!$adminPassword',
            'customData': base64Encode(utf8.encode(cloudInit)),
            'linuxConfiguration': {'disablePasswordAuthentication': false},
          },
          'storageProfile': {
            'imageReference': {
              'publisher': 'Canonical',
              'offer': '0001-com-ubuntu-server-jammy',
              'sku': '22_04-lts-gen2',
              'version': 'latest',
            },
            'osDisk': {
              'createOption': 'FromImage',
              'managedDisk': {'storageAccountType': 'StandardSSD_LRS'},
              'diskSizeGB': 30,
            },
          },
          'networkProfile': {
            'networkInterfaces': [
              {'id': nicId},
            ],
          },
        },
      },
      timeout: const Duration(minutes: 5),
    );

    // 7. Poll until running
    onProgress('Waiting for VM to boot...');
    String vmIp = '';
    for (int i = 0; i < 60; i++) {
      await Future.delayed(const Duration(seconds: 5));
      final info = await _getVmStatus(sub);
      if (info.isRunning && info.ip.isNotEmpty) {
        vmIp = info.ip;
        break;
      }
    }

    if (vmIp.isEmpty) {
      final pipResp = await http.get(
        Uri.parse('$_armBase$pipId?api-version=$_netApiVersion'),
        headers: _headers,
      );
      if (pipResp.statusCode == 200) {
        final pipData = jsonDecode(pipResp.body);
        vmIp = pipData['properties']?['ipAddress'] ?? '';
      }
    }

    // 8. Wait for cloud-init + relay
    onProgress('Waiting for relay to start...');
    await Future.delayed(const Duration(seconds: 30));

    final result = VmInfo(
      ip: vmIp,
      status: 'VM running',
      token: relayToken,
      subscriptionId: sub,
    );
    await _saveVmInfo(result);
    return result;
  }

  // -- VM status --

  Future<VmInfo> getVmStatus() async {
    final saved = await loadVmInfo();
    if (!saved.exists || saved.subscriptionId.isEmpty) return saved;
    try {
      final info = await _getVmStatus(saved.subscriptionId);
      final updated = VmInfo(
        ip: info.ip.isNotEmpty ? info.ip : saved.ip,
        status: info.status,
        token: saved.token,
        subscriptionId: saved.subscriptionId,
      );
      await _saveVmInfo(updated);
      return updated;
    } catch (_) {
      return saved;
    }
  }

  Future<VmInfo> _getVmStatus(String sub) async {
    final path =
        '/subscriptions/$sub/resourceGroups/$_rgName/providers/Microsoft.Compute/virtualMachines/$_vmName';
    final resp = await http.get(
      Uri.parse(
          '$_armBase$path?api-version=$_apiVersion&\$expand=instanceView'),
      headers: _headers,
    );
    if (resp.statusCode == 404) return VmInfo.notFound();
    if (resp.statusCode != 200) return VmInfo.notFound();

    final data = jsonDecode(resp.body);
    final statuses =
        data['properties']?['instanceView']?['statuses'] as List? ?? [];
    String vmStatus = 'unknown';
    for (final s in statuses) {
      final code = s['code'] as String? ?? '';
      if (code.startsWith('PowerState/')) {
        vmStatus = s['displayStatus'] as String? ?? 'unknown';
      }
    }

    String ip = '';
    final nics = data['properties']?['networkProfile']?['networkInterfaces']
            as List? ??
        [];
    if (nics.isNotEmpty) {
      final nicId = nics[0]['id'] as String? ?? '';
      if (nicId.isNotEmpty) {
        ip = await _getPublicIpFromNic(nicId);
      }
    }

    final saved = await loadVmInfo();
    return VmInfo(
      ip: ip,
      status: vmStatus,
      token: saved.token,
      subscriptionId: sub,
    );
  }

  Future<String> _getPublicIpFromNic(String nicId) async {
    try {
      final nicResp = await http.get(
        Uri.parse('$_armBase$nicId?api-version=$_netApiVersion'),
        headers: _headers,
      );
      if (nicResp.statusCode != 200) return '';
      final nicData = jsonDecode(nicResp.body);
      final ipConfigs =
          nicData['properties']?['ipConfigurations'] as List? ?? [];
      if (ipConfigs.isEmpty) return '';
      final pipId = ipConfigs[0]['properties']?['publicIPAddress']?['id']
              as String? ??
          '';
      if (pipId.isEmpty) return '';

      final pipResp = await http.get(
        Uri.parse('$_armBase$pipId?api-version=$_netApiVersion'),
        headers: _headers,
      );
      if (pipResp.statusCode != 200) return '';
      final pipData = jsonDecode(pipResp.body);
      return pipData['properties']?['ipAddress'] ?? '';
    } catch (_) {
      return '';
    }
  }

  // -- VM lifecycle --

  Future<bool> startVm() async {
    final saved = await loadVmInfo();
    if (!saved.exists) return false;
    final path =
        '/subscriptions/${saved.subscriptionId}/resourceGroups/$_rgName'
        '/providers/Microsoft.Compute/virtualMachines/$_vmName/start';
    final resp = await http.post(
      Uri.parse('$_armBase$path?api-version=$_apiVersion'),
      headers: _headers,
    );
    return resp.statusCode == 200 || resp.statusCode == 202;
  }

  Future<bool> deallocateVm() async {
    final saved = await loadVmInfo();
    if (!saved.exists) return false;
    final path =
        '/subscriptions/${saved.subscriptionId}/resourceGroups/$_rgName'
        '/providers/Microsoft.Compute/virtualMachines/$_vmName/deallocate';
    final resp = await http.post(
      Uri.parse('$_armBase$path?api-version=$_apiVersion'),
      headers: _headers,
    );
    return resp.statusCode == 200 || resp.statusCode == 202;
  }

  Future<bool> deleteAll() async {
    final saved = await loadVmInfo();
    if (!saved.exists) return false;
    final path =
        '/subscriptions/${saved.subscriptionId}/resourceGroups/$_rgName';
    final resp = await http.delete(
      Uri.parse('$_armBase$path?api-version=2024-03-01'),
      headers: _headers,
    );
    if (resp.statusCode == 200 || resp.statusCode == 202) {
      await clearVmInfo();
      return true;
    }
    return false;
  }

  // -- Helpers --

  Future<void> _armPut(
    String step,
    String path, {
    required Map<String, dynamic> body,
    Duration timeout = const Duration(seconds: 60),
  }) async {
    final r = await http
        .put(
          Uri.parse('$_armBase$path'),
          headers: _headers,
          body: jsonEncode(body),
        )
        .timeout(timeout);
    if (r.statusCode >= 200 && r.statusCode < 300) return;

    String detail;
    try {
      final decoded = jsonDecode(r.body);
      if (decoded is Map<String, dynamic>) {
        final err = decoded['error'];
        if (err is Map<String, dynamic>) {
          detail = err['message'] as String? ??
              err['code'] as String? ??
              r.body;
        } else {
          detail = decoded['message'] as String? ?? r.body;
        }
      } else {
        detail = r.body;
      }
    } catch (_) {
      detail = r.body.length > 240 ? '${r.body.substring(0, 240)}…' : r.body;
    }
    throw AzureProvisionException('$step failed (${r.statusCode}): $detail');
  }

  String _buildCloudInit(String relayToken) {
    return '''#cloud-config
write_files:
  - path: /home/$_adminUser/relay.py
    permissions: '0755'
    content: |
      import asyncio, hmac, logging, ssl, struct, subprocess, time
      from collections import defaultdict
      from pathlib import Path

      logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
      logger = logging.getLogger(__name__)

      MAX_CONNECTIONS = 50
      RATE_WINDOW = 60
      MAX_PER_IP = 10
      BAN_AFTER_FAILURES = 5
      BAN_DURATION = 300

      ip_conns = defaultdict(int)
      ip_failures = defaultdict(list)
      banned = {}

      def gen_cert(cert, key):
          if Path(cert).exists() and Path(key).exists(): return
          subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-keyout",key,"-out",cert,"-days","365","-nodes","-subj","/CN=relay"], check=True, capture_output=True)

      class Relay:
          def __init__(self, host, port, cert, key, password=None):
              self.host, self.port, self.cert, self.key = host, port, cert, key
              self.pw = password.encode() if password else None
              self.active = 0

          async def start(self):
              ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
              ctx.load_cert_chain(self.cert, self.key)
              ctx.minimum_version = ssl.TLSVersion.TLSv1_2
              srv = await asyncio.start_server(self._handle, self.host, self.port, ssl=ctx)
              logger.info("Relay on %s:%d (max %d conns)", self.host, self.port, MAX_CONNECTIONS)
              async with srv: await srv.serve_forever()

          async def _handle(self, r, w):
              peer = w.get_extra_info("peername")
              ip = peer[0] if peer else "unknown"
              now = time.time()

              if ip in banned and now < banned[ip]:
                  w.close(); return
              if ip in banned: del banned[ip]

              if self.active >= MAX_CONNECTIONS:
                  logger.warning("Max connections reached, rejecting %s", ip)
                  w.close(); return

              if ip_conns[ip] >= MAX_PER_IP:
                  logger.warning("Per-IP limit for %s", ip)
                  w.close(); return

              ip_conns[ip] += 1
              self.active += 1
              try:
                  if self.pw:
                      pl = (await asyncio.wait_for(r.readexactly(1), timeout=10))[0]
                      pw = await asyncio.wait_for(r.readexactly(pl), timeout=10)
                      if not hmac.compare_digest(pw, self.pw):
                          logger.warning("Auth failed from %s", ip)
                          ip_failures[ip].append(now)
                          recent = [t for t in ip_failures[ip] if now - t < RATE_WINDOW]
                          ip_failures[ip] = recent
                          if len(recent) >= BAN_AFTER_FAILURES:
                              banned[ip] = now + BAN_DURATION
                              logger.warning("Banned %s for %ds", ip, BAN_DURATION)
                          w.write(b"\\x01"); await w.drain(); return

                  hl = (await asyncio.wait_for(r.readexactly(1), timeout=10))[0]
                  host = (await asyncio.wait_for(r.readexactly(hl), timeout=10)).decode()
                  port = struct.unpack("!H", await asyncio.wait_for(r.readexactly(2), timeout=10))[0]

                  if port == 0 or not host:
                      w.write(b"\\x01"); await w.drain(); return

                  logger.info("%s -> %s:%d (active: %d)", ip, host, port, self.active)
                  try:
                      dr, dw = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=15)
                  except Exception:
                      w.write(b"\\x01"); await w.drain(); return
                  w.write(b"\\x00"); await w.drain()
                  await asyncio.gather(self._pipe(r, dw), self._pipe(dr, w))
              except Exception: pass
              finally:
                  self.active -= 1
                  ip_conns[ip] = max(0, ip_conns[ip] - 1)
                  try: w.close()
                  except: pass

          async def _pipe(self, r, w):
              try:
                  while True:
                      d = await r.read(65536)
                      if not d: break
                      w.write(d); await w.drain()
              except Exception: pass
              finally:
                  try: w.close()
                  except: pass

      if __name__ == "__main__":
          gen_cert("/home/$_adminUser/cert.pem", "/home/$_adminUser/key.pem")
          asyncio.run(Relay("0.0.0.0", $_relayPort, "/home/$_adminUser/cert.pem", "/home/$_adminUser/key.pem", "$relayToken").start())

  - path: /etc/systemd/system/hashsec-relay.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Hash-Sec Tunnel Relay
      After=network.target
      [Service]
      Type=simple
      User=$_adminUser
      ExecStart=/usr/bin/python3 /home/$_adminUser/relay.py
      Restart=always
      RestartSec=5
      LimitNOFILE=65536
      [Install]
      WantedBy=multi-user.target

runcmd:
  - openssl req -x509 -newkey rsa:2048 -keyout /home/$_adminUser/key.pem -out /home/$_adminUser/cert.pem -days 365 -nodes -subj "/CN=relay"
  - chown $_adminUser:$_adminUser /home/$_adminUser/*.pem
  - chmod 600 /home/$_adminUser/key.pem
  - ufw default deny incoming
  - ufw allow $_relayPort/tcp
  - ufw --force enable
  - systemctl daemon-reload
  - systemctl enable hashsec-relay
  - systemctl start hashsec-relay
  - passwd -l $_adminUser
''';
  }
}
