# Mobile Integration Guide

## Android + Flutter Architecture

### Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Android App                              │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                   Flutter UI                          │  │
│  │  - Connect / Disconnect button                        │  │
│  │  - Status indicator (connected/disconnected)          │  │
│  │  - Bandwidth graph                                    │  │
│  │  - Server selection                                   │  │
│  │  - SNI configuration                                  │  │
│  └───────────────┬───────────────────────────────────────┘  │
│                  │ HTTP (localhost:8080)                      │
│                  ▼                                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │           Nexus Mobile API (REST + WebSocket)         │  │
│  │           Running in background service               │  │
│  └───────────────┬───────────────────────────────────────┘  │
│                  │                                           │
│  ┌───────────────▼───────────────────────────────────────┐  │
│  │            Nexus Client (Python / Go)                 │  │
│  │  SOCKS5 Server ──▶ TunnelManager ──▶ WSS Transport   │  │
│  └───────────────┬───────────────────────────────────────┘  │
│                  │ SOCKS5 (127.0.0.1:1080)                   │
│  ┌───────────────▼───────────────────────────────────────┐  │
│  │           tun2socks (Go library)                      │  │
│  └───────────────┬───────────────────────────────────────┘  │
│                  │ Raw IP packets                            │
│  ┌───────────────▼───────────────────────────────────────┐  │
│  │           VpnService (Android OS)                     │  │
│  │  - TUN file descriptor                                │  │
│  │  - Routes all traffic through Nexus                   │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │
         │  WSS + TLS (SNI spoofed)
         ▼
    Relay Server
```

### Android VpnService Setup (Kotlin)

```kotlin
// NexusVpnService.kt
class NexusVpnService : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private var nexusProcess: Process? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startVpn()
            ACTION_STOP -> stopVpn()
        }
        return START_STICKY
    }

    private fun startVpn() {
        val builder = Builder()
            .setSession("Hamieh Tunnel")
            .addAddress("10.88.0.1", 24)
            .addRoute("0.0.0.0", 0)        // Capture ALL traffic
            .addDnsServer("1.1.1.1")
            .addDnsServer("8.8.8.8")
            .setMtu(1500)

        // Exclude hamieh itself to prevent routing loop
        builder.addDisallowedApplication(packageName)

        vpnInterface = builder.establish()

        // Start tun2socks with the TUN file descriptor
        startTun2Socks(vpnInterface!!.fd)

        // Start hamieh client (Python via Chaquopy, or Go binary)
        startNexusClient()
    }

    private fun startTun2Socks(tunFd: Int) {
        // tun2socks reads from TUN fd, routes to SOCKS5 at 127.0.0.1:1080
        val cmd = arrayOf(
            "/data/data/$packageName/files/tun2socks",
            "-device", "fd://$tunFd",
            "-proxy", "socks5://127.0.0.1:1080"
        )
        nexusProcess = ProcessBuilder(*cmd).start()
    }

    private fun startNexusClient() {
        // Via Chaquopy (Python runtime for Android):
        // Python.getModule("cli.main").callAttr("start_from_android", configJson)

        // Or launch Go binary:
        // val cmd = arrayOf("/data/data/$packageName/files/hamieh-client", "--config", configPath)
    }

    private fun stopVpn() {
        nexusProcess?.destroy()
        vpnInterface?.close()
        stopSelf()
    }

    companion object {
        const val ACTION_START = "com.nexustunnel.START"
        const val ACTION_STOP = "com.nexustunnel.STOP"
    }
}
```

**AndroidManifest.xml:**
```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />

<service
    android:name=".NexusVpnService"
    android:permission="android.permission.BIND_VPN_SERVICE"
    android:exported="false">
    <intent-filter>
        <action android:name="android.net.VpnService" />
    </intent-filter>
</service>
```

---

### Flutter App Integration

#### Dependencies (pubspec.yaml)

```yaml
dependencies:
  flutter:
    sdk: flutter
  http: ^1.1.0
  web_socket_channel: ^2.4.0
  fl_chart: ^0.66.0          # Bandwidth graphs
  shared_preferences: ^2.2.0
  provider: ^6.1.0
```

#### Nexus API Client (Dart)

```dart
// lib/services/nexus_api.dart
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

class NexusApi {
  static const _base = 'http://127.0.0.1:8080';
  final String _token;

  NexusApi(this._token);

  Map<String, String> get _headers => {
    'Authorization': 'Bearer $_token',
    'Content-Type': 'application/json',
  };

  Future<Map<String, dynamic>> start() async {
    final resp = await http.post(
      Uri.parse('$_base/api/tunnel/start'),
      headers: _headers,
    );
    return jsonDecode(resp.body);
  }

  Future<Map<String, dynamic>> stop() async {
    final resp = await http.post(
      Uri.parse('$_base/api/tunnel/stop'),
      headers: _headers,
    );
    return jsonDecode(resp.body);
  }

  Future<Map<String, dynamic>> status() async {
    final resp = await http.get(
      Uri.parse('$_base/api/tunnel/status'),
      headers: _headers,
    );
    return jsonDecode(resp.body);
  }

  Future<Map<String, dynamic>> updateConfig({
    String? sni,
    String? relayHost,
    int? relayPort,
  }) async {
    final body = <String, dynamic>{};
    if (sni != null) body['sni'] = sni;
    if (relayHost != null) body['relay_host'] = relayHost;
    if (relayPort != null) body['relay_port'] = relayPort;

    final resp = await http.post(
      Uri.parse('$_base/api/config'),
      headers: _headers,
      body: jsonEncode(body),
    );
    return jsonDecode(resp.body);
  }

  /// Real-time status stream (1-second updates)
  Stream<Map<String, dynamic>> statusStream() {
    final channel = WebSocketChannel.connect(
      Uri.parse('ws://127.0.0.1:8080/ws/status'),
    );
    return channel.stream.map((msg) => jsonDecode(msg as String));
  }

  /// Real-time log stream
  Stream<Map<String, dynamic>> logStream() {
    final channel = WebSocketChannel.connect(
      Uri.parse('ws://127.0.0.1:8080/ws/logs'),
    );
    return channel.stream.map((msg) => jsonDecode(msg as String));
  }
}
```

#### Main UI (Flutter)

```dart
// lib/main.dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'services/nexus_api.dart';

void main() {
  runApp(
    ChangeNotifierProvider(
      create: (_) => TunnelState(),
      child: const NexusApp(),
    ),
  );
}

class TunnelState extends ChangeNotifier {
  bool isConnected = false;
  Map<String, dynamic> statusData = {};
  final _api = NexusApi('YOUR_TOKEN_HERE');

  Future<void> toggle() async {
    if (isConnected) {
      await _api.stop();
    } else {
      await _api.start();
    }
    await refresh();
  }

  Future<void> refresh() async {
    statusData = await _api.status();
    isConnected = statusData['running'] ?? false;
    notifyListeners();
  }

  Stream<Map<String, dynamic>> get statusStream => _api.statusStream();
}

class NexusApp extends StatelessWidget {
  const NexusApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Hamieh Tunnel',
      theme: ThemeData.dark().copyWith(
        colorScheme: ColorScheme.dark(
          primary: Colors.cyan,
          secondary: Colors.cyanAccent,
        ),
      ),
      home: const HomeScreen(),
    );
  }
}

class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<TunnelState>();
    final m = state.statusData['metrics'] ?? {};
    final bw = m['bandwidth'] ?? {};

    return Scaffold(
      appBar: AppBar(title: const Text('Hamieh Tunnel')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          children: [
            // Connection toggle button
            GestureDetector(
              onTap: state.toggle,
              child: Container(
                width: 160,
                height: 160,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: state.isConnected ? Colors.cyan : Colors.grey[800],
                  boxShadow: state.isConnected
                    ? [BoxShadow(color: Colors.cyan.withOpacity(0.5), blurRadius: 30)]
                    : [],
                ),
                child: Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        state.isConnected ? Icons.shield : Icons.shield_outlined,
                        size: 48,
                        color: Colors.white,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        state.isConnected ? 'CONNECTED' : 'CONNECT',
                        style: const TextStyle(
                          fontWeight: FontWeight.bold,
                          color: Colors.white,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),

            const SizedBox(height: 32),

            // Stats
            if (state.isConnected) ...[
              _StatRow('Relay', state.statusData['relay'] ?? '—'),
              _StatRow('SNI', state.statusData['sni'] ?? '—'),
              _StatRow('Uptime', '${state.statusData['uptime_seconds']?.toStringAsFixed(0) ?? 0}s'),
              _StatRow('Sent', '${(bw['sent_mb'] ?? 0).toStringAsFixed(2)} MB'),
              _StatRow('Received', '${(bw['recv_mb'] ?? 0).toStringAsFixed(2)} MB'),
              _StatRow('Connections', '${m['connections']?['active'] ?? 0} active'),
            ],
          ],
        ),
      ),
    );
  }
}

class _StatRow extends StatelessWidget {
  const _StatRow(this.label, this.value);
  final String label, value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label, style: TextStyle(color: Colors.grey[400])),
          Text(value, style: const TextStyle(fontWeight: FontWeight.w500)),
        ],
      ),
    );
  }
}
```

---

## Mobile API Reference

Base URL: `http://127.0.0.1:8080`

### REST Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /api/tunnel/start | Bearer | Start the tunnel |
| POST | /api/tunnel/stop | Bearer | Stop the tunnel |
| GET | /api/tunnel/status | — | Status + metrics |
| GET | /api/tunnel/logs?limit=N | Bearer | Recent log lines |
| POST | /api/config | Bearer | Update config |
| GET | /api/health | — | Liveness check |

### WebSocket Endpoints

| Path | Auth | Description |
|------|------|-------------|
| /ws/status | — | Real-time status (1s updates) |
| /ws/logs | Bearer | Real-time log stream |

### Status Response

```json
{
  "running": true,
  "uptime_seconds": 142.3,
  "socks5": { "host": "127.0.0.1", "port": 1080 },
  "transport": "wss",
  "relay": "1.2.3.4:8443",
  "sni": "teams.microsoft.com",
  "metrics": {
    "uptime_s": 142.3,
    "connections": { "active": 3, "total": 47, "failed": 0 },
    "bandwidth": { "sent_mb": 12.4, "recv_mb": 87.2 },
    "tunnel": { "active": 1, "rotations": 0, "errors": 0 }
  }
}
```
