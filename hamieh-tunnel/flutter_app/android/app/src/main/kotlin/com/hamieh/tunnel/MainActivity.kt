package com.hamieh.tunnel

import android.content.Intent
import android.net.Uri
import android.net.VpnService
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val vpnChannel = "com.hamieh.tunnel/vpn"
    private val authChannel = "com.hamieh.tunnel/auth"
    private var authMethodChannel: MethodChannel? = null
    private var vpnPermissionResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        authMethodChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, authChannel)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, vpnChannel).setMethodCallHandler { call, result ->
            when (call.method) {
                "requestVpnPermission" -> requestVpnPermission(result)
                "start" -> {
                    val relayHost = call.argument<String>("relay_host") ?: ""
                    val relayPort = call.argument<Int>("relay_port") ?: 9443
                    val sni = call.argument<String>("sni") ?: "teams.microsoft.com"
                    val token = call.argument<String>("token") ?: ""

                    val vpnIntent = VpnService.prepare(this)
                    if (vpnIntent != null) {
                        startActivityForResult(vpnIntent, VPN_PERMISSION_CODE)
                        pendingVpnStart = VpnStartParams(relayHost, relayPort, sni, token)
                        vpnPermissionResult = result
                    } else {
                        startVpnService(relayHost, relayPort, sni, token)
                        result.success(true)
                    }
                }
                "stop" -> {
                    val intent = Intent(this, HamiehVpnService::class.java)
                    intent.action = HamiehVpnService.ACTION_STOP
                    startService(intent)
                    result.success(true)
                }
                else -> result.notImplemented()
            }
        }

        // Cold start: app opened via hamieh://auth deep link
        deliverAuthFromIntent(intent)
    }

    private fun startVpnService(relayHost: String, relayPort: Int, sni: String, token: String) {
        val intent = Intent(this, HamiehVpnService::class.java).apply {
            action = HamiehVpnService.ACTION_START
            putExtra("relay_host", relayHost)
            putExtra("relay_port", relayPort)
            putExtra("sni", sni)
            putExtra("token", token)
        }
        startService(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        deliverAuthFromIntent(intent)
    }

    private fun deliverAuthFromIntent(intent: Intent?) {
        val uri: Uri? = intent?.data
        if (uri == null || uri.scheme != "hamieh" || uri.host != "auth") return

        val code = uri.getQueryParameter("code")
        if (code.isNullOrEmpty()) return

        val state = uri.getQueryParameter("state") ?: ""
        authMethodChannel?.invokeMethod(
            "onAuthCallback",
            mapOf("code" to code, "state" to state),
        )
    }

    private fun requestVpnPermission(result: MethodChannel.Result) {
        val intent = VpnService.prepare(this)
        if (intent == null) {
            result.success(true)
        } else {
            vpnPermissionResult = result
            startActivityForResult(intent, VPN_PERMISSION_CODE)
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VPN_PERMISSION_CODE) {
            val granted = resultCode == RESULT_OK
            vpnPermissionResult?.success(granted)
            vpnPermissionResult = null

            if (granted && pendingVpnStart != null) {
                val p = pendingVpnStart!!
                startVpnService(p.relayHost, p.relayPort, p.sni, p.token)
                pendingVpnStart = null
            }
        }
    }

    private var pendingVpnStart: VpnStartParams? = null

    data class VpnStartParams(
        val relayHost: String,
        val relayPort: Int,
        val sni: String,
        val token: String,
    )

    companion object {
        const val VPN_PERMISSION_CODE = 1001
    }
}
