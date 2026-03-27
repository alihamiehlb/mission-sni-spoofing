package com.hamieh.tunnel

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import java.io.File

class HamiehVpnService : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private var tun2socksProcess: Process? = null
    private var hamiehClientProcess: Process? = null

    companion object {
        const val ACTION_START = "com.hamieh.tunnel.START"
        const val ACTION_STOP = "com.hamieh.tunnel.STOP"
        const val CHANNEL_ID = "hashsec_vpn"
        const val NOTIF_ID = 1
        private const val TAG = "HashSecVPN"
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return when (intent?.action) {
            ACTION_START -> {
                val relayHost = intent.getStringExtra("relay_host") ?: ""
                val relayPort = intent.getIntExtra("relay_port", 9443)
                val sni = intent.getStringExtra("sni") ?: "teams.microsoft.com"
                val token = intent.getStringExtra("token") ?: ""
                startVpn(relayHost, relayPort, sni, token)
                START_STICKY
            }
            ACTION_STOP -> {
                stopVpn()
                START_NOT_STICKY
            }
            else -> START_NOT_STICKY
        }
    }

    private fun startVpn(relayHost: String, relayPort: Int, sni: String, token: String) {
        startForeground(NOTIF_ID, buildNotification("Connecting..."))

        val builder = Builder()
            .setSession("Hash-Sec Tunnel")
            .addAddress("10.88.0.1", 24)
            .addRoute("0.0.0.0", 0)
            .addDnsServer("1.1.1.1")
            .addDnsServer("8.8.8.8")
            .setMtu(1500)
            .addDisallowedApplication(packageName)

        vpnInterface = builder.establish()
        if (vpnInterface == null) {
            Log.e(TAG, "Failed to establish VPN interface")
            stopSelf()
            return
        }

        val tunFd = vpnInterface!!.fd
        Log.i(TAG, "TUN device established (fd=$tunFd)")

        writeRelayConfig(relayHost, relayPort, sni, token)
        startHamiehClient()
        Thread.sleep(800)
        startTun2Socks(tunFd)

        updateNotification("Protected via Azure ($relayHost)")
        Log.i(TAG, "Hash-Sec Tunnel active")
    }

    private fun writeRelayConfig(relayHost: String, relayPort: Int, sni: String, token: String) {
        val configFile = File(filesDir, "relay.conf")
        configFile.writeText(
            "relay_host: $relayHost\n" +
            "relay_port: $relayPort\n" +
            "sni: $sni\n" +
            "token: $token\n"
        )
        Log.i(TAG, "Config written to ${configFile.absolutePath}")
    }

    private fun startHamiehClient() {
        val nativeDir = applicationInfo.nativeLibraryDir
        val binPath = "$nativeDir/libhamieh.so"

        if (!File(binPath).exists()) {
            Log.e(TAG, "hamieh-client binary not found at $binPath")
            return
        }

        val configPath = File(filesDir, "relay.conf").absolutePath

        val cmd = arrayOf(
            binPath,
            "--config", configPath,
            "--listen", "127.0.0.1:1080"
        )

        hamiehClientProcess = ProcessBuilder(*cmd)
            .redirectErrorStream(true)
            .start()

        Thread {
            try {
                val reader = hamiehClientProcess!!.inputStream.bufferedReader()
                var line = reader.readLine()
                while (line != null) {
                    Log.d(TAG, "hamieh-client: $line")
                    line = reader.readLine()
                }
            } catch (_: Exception) {}
        }.start()

        Log.i(TAG, "hamieh-client started with config file")
    }

    private fun startTun2Socks(tunFd: Int) {
        val nativeDir = applicationInfo.nativeLibraryDir
        val binPath = "$nativeDir/libtun2socks.so"

        if (!File(binPath).exists()) {
            Log.e(TAG, "tun2socks binary not found at $binPath")
            return
        }

        val cmd = arrayOf(
            binPath,
            "--device", "fd://$tunFd",
            "--proxy", "socks5://127.0.0.1:1080",
            "--loglevel", "warn"
        )
        tun2socksProcess = ProcessBuilder(*cmd)
            .redirectErrorStream(true)
            .start()

        Thread {
            try {
                val reader = tun2socksProcess!!.inputStream.bufferedReader()
                var line = reader.readLine()
                while (line != null) {
                    Log.d(TAG, "tun2socks: $line")
                    line = reader.readLine()
                }
            } catch (_: Exception) {}
        }.start()

        Log.i(TAG, "tun2socks started")
    }

    private fun stopVpn() {
        tun2socksProcess?.destroy()
        hamiehClientProcess?.destroy()
        vpnInterface?.close()
        tun2socksProcess = null
        hamiehClientProcess = null
        vpnInterface = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        Log.i(TAG, "Hash-Sec Tunnel stopped")
    }

    override fun onDestroy() {
        stopVpn()
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Hash-Sec Tunnel VPN",
                NotificationManager.IMPORTANCE_LOW
            ).apply { description = "Hash-Sec Tunnel VPN session" }
            getSystemService(NotificationManager::class.java)
                .createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Hash-Sec Tunnel")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIF_ID, buildNotification(text))
    }
}
