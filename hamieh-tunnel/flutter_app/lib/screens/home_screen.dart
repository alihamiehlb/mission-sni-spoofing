import 'dart:async';
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_animate/flutter_animate.dart';
import 'package:gap/gap.dart';
import 'package:provider/provider.dart';
import '../main.dart';
import '../services/azure_service.dart';
import '../theme/app_theme.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with TickerProviderStateMixin {
  late AppState _appState;
  VmInfo _vm = VmInfo.notFound();
  bool _connected = false;
  bool _loading = false;
  bool _creating = false;
  String _progressMsg = '';
  double _usedGb = 0.0;
  static const double _totalGb = 20.0;
  Timer? _timer;
  late AnimationController _pulseCtrl;

  List<Map<String, String>> _subscriptions = [];
  String? _selectedSubId;

  static const _vpnChannel = MethodChannel('com.hamieh.tunnel/vpn');

  @override
  void initState() {
    super.initState();
    _appState = context.read<AppState>();
    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 3),
    )..repeat(reverse: true);
    _init();
  }

  @override
  void dispose() {
    _timer?.cancel();
    _pulseCtrl.dispose();
    super.dispose();
  }

  Future<void> _init() async {
    await _appState.ensureValidToken();
    await _loadUsage();
    await _loadVm();
    await _loadSubscriptions();
    _startPolling();
  }

  Future<void> _loadUsage() async {
    final mb = await _appState.api.getTodayUsageMb();
    if (mounted) setState(() => _usedGb = mb / 1024.0);
  }

  Future<void> _loadVm() async {
    final az = _appState.azure;
    if (az == null) return;
    try {
      final info = await az.getVmStatus();
      if (mounted) setState(() => _vm = info);
    } catch (_) {
      final saved = await az.loadVmInfo();
      if (mounted) setState(() => _vm = saved);
    }
  }

  Future<void> _loadSubscriptions() async {
    final az = _appState.azure;
    if (az == null) return;
    try {
      final subs = await az.listSubscriptions();
      if (mounted) {
        setState(() {
          _subscriptions = subs;
          if (subs.isNotEmpty) {
            _selectedSubId = _vm.subscriptionId.isNotEmpty
                ? _vm.subscriptionId
                : subs.first['id'];
          }
        });
      }
    } catch (_) {}
  }

  void _startPolling() {
    _timer = Timer.periodic(const Duration(seconds: 15), (_) => _loadVm());
  }

  // -- VM Management --

  Future<void> _createTunnel() async {
    if (_selectedSubId == null || _selectedSubId!.isEmpty) {
      _showSnack('Select an Azure subscription first', icon: Icons.warning_rounded, color: AppTheme.warning);
      return;
    }
    setState(() { _creating = true; _progressMsg = 'Starting...'; });

    try {
      await _appState.ensureValidToken();
      final az = _appState.azure!;
      final info = await az.createTunnel(
        subscriptionId: _selectedSubId!,
        onProgress: (msg) {
          if (mounted) setState(() => _progressMsg = msg);
        },
      );
      if (mounted) setState(() { _vm = info; _creating = false; });
    } catch (e) {
      if (mounted) {
        setState(() { _creating = false; _progressMsg = ''; });
        _showSnack('Failed: $e', icon: Icons.error_outline, color: AppTheme.danger);
      }
    }
  }

  Future<void> _startVm() async {
    setState(() => _loading = true);
    await _appState.ensureValidToken();
    await _appState.azure?.startVm();
    await Future.delayed(const Duration(seconds: 10));
    await _loadVm();
    setState(() => _loading = false);
  }

  Future<void> _deallocateVm() async {
    setState(() => _loading = true);
    if (_connected) await _disconnectVpn();
    await _appState.ensureValidToken();
    await _appState.azure?.deallocateVm();
    await Future.delayed(const Duration(seconds: 3));
    await _loadVm();
    setState(() => _loading = false);
    _showSnack('VM deallocated (no charges)', icon: Icons.pause_circle_outline, color: AppTheme.success);
  }

  Future<void> _deleteAll() async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surfaceCard,
        title: const Text('Delete Everything?', style: TextStyle(color: Colors.white)),
        content: const Text(
          'This will permanently delete the VM and all Azure resources. You can create a new one anytime.',
          style: TextStyle(color: Colors.white70),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete', style: TextStyle(color: AppTheme.danger)),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    setState(() => _loading = true);
    if (_connected) await _disconnectVpn();
    await _appState.ensureValidToken();
    await _appState.azure?.deleteAll();
    setState(() { _vm = VmInfo.notFound(); _loading = false; });
    _showSnack('All resources deleted', icon: Icons.delete_outline, color: AppTheme.primary);
  }

  // -- VPN --

  Future<void> _connectVpn() async {
    if (_vm.ip.isEmpty) return;

    if (!_isInTimeWindow()) {
      _showSnack('Bundle active only 7:30 AM – 2:00 PM', icon: Icons.schedule, color: AppTheme.warning);
      return;
    }
    if (_usedGb >= _totalGb) {
      _showSnack('20 GB limit reached for today', icon: Icons.data_usage, color: AppTheme.danger);
      return;
    }

    setState(() => _loading = true);
    try {
      await _vpnChannel.invokeMethod('start', {
        'relay_host': _vm.ip,
        'relay_port': 9443,
        'sni': 'teams.microsoft.com',
        'token': _vm.token,
      });
      setState(() => _connected = true);
    } catch (e) {
      _showSnack('VPN start failed: $e', color: AppTheme.danger);
    }
    setState(() => _loading = false);
  }

  Future<void> _disconnectVpn() async {
    try {
      await _vpnChannel.invokeMethod('stop');
    } catch (_) {}
    setState(() => _connected = false);
  }

  // -- Helpers --

  bool _isInTimeWindow() {
    final now = TimeOfDay.now();
    final nowMin = now.hour * 60 + now.minute;
    return nowMin >= 450 && nowMin <= 840; // 7:30 - 14:00
  }

  String _timeRemaining() {
    final now = DateTime.now();
    final end = DateTime(now.year, now.month, now.day, 14, 0);
    if (now.isAfter(end)) return 'Expired';
    final diff = end.difference(now);
    return '${diff.inHours}h ${diff.inMinutes % 60}m left';
  }

  void _showSnack(String msg, {IconData? icon, Color? color}) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Row(children: [
        if (icon != null) ...[Icon(icon, color: Colors.white, size: 18), const SizedBox(width: 10)],
        Expanded(child: Text(msg)),
      ]),
      backgroundColor: color ?? AppTheme.surfaceElevated,
      behavior: SnackBarBehavior.floating,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final inWindow = _isInTimeWindow();
    final usagePercent = (_usedGb / _totalGb).clamp(0.0, 1.0);

    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Color(0xFF0A192F), Color(0xFF080B14), Color(0xFF080B14)],
          ),
        ),
        child: SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Column(
              children: [
                const Gap(8),
                _buildHeader(),
                const Gap(24),

                if (_creating)
                  _buildCreatingCard()
                else if (!_vm.exists)
                  _buildSetupCard()
                else if (_vm.isDeallocated)
                  _buildDeallocatedCard()
                else if (_vm.isRunning && !_connected)
                  _buildReadyCard(usagePercent)
                else if (_connected)
                  _buildConnectedCard(usagePercent, inWindow),

                const Gap(16),
                if (_vm.exists) ...[
                  _buildUsageCard(usagePercent),
                  const Gap(12),
                  _buildTimeCard(inWindow),
                  const Gap(12),
                  _buildVmInfoCard(),
                ],
                const Gap(32),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Row(
      children: [
        Container(
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(
            color: AppTheme.primary.withAlpha(20),
            borderRadius: BorderRadius.circular(12),
          ),
          child: const Icon(Icons.shield_rounded, color: AppTheme.primary, size: 22),
        ),
        const SizedBox(width: 12),
        const Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Hash-Sec Tunnel', style: TextStyle(color: Colors.white, fontSize: 17, fontWeight: FontWeight.w700)),
            Text('Azure VM Tunnel', style: TextStyle(color: Colors.white38, fontSize: 11)),
          ],
        ),
        const Spacer(),
        IconButton(
          onPressed: () async {
            await _appState.logout();
          },
          icon: const Icon(Icons.logout_rounded, color: Colors.white38, size: 20),
        ),
      ],
    );
  }

  // -- State 1: No VM --

  Widget _buildSetupCard() {
    return Column(
      children: [
        const Gap(24),
        Icon(Icons.cloud_off_rounded, size: 64, color: Colors.white.withAlpha(40)),
        const Gap(16),
        const Text('No Tunnel Created', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w700)),
        const Gap(8),
        Text('Create an Azure VM to start tunneling', style: TextStyle(color: Colors.white.withAlpha(120), fontSize: 13)),
        const Gap(28),

        if (_subscriptions.isNotEmpty) ...[
          _glassCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Azure Subscription', style: TextStyle(color: Colors.white70, fontSize: 13, fontWeight: FontWeight.w600)),
                const Gap(8),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14),
                  decoration: BoxDecoration(
                    color: AppTheme.surface.withAlpha(200),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: DropdownButton<String>(
                    value: _selectedSubId,
                    isExpanded: true,
                    dropdownColor: AppTheme.surfaceCard,
                    underline: const SizedBox(),
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    items: _subscriptions.map((s) => DropdownMenuItem(
                      value: s['id'],
                      child: Text(s['name'] ?? s['id']!, overflow: TextOverflow.ellipsis),
                    )).toList(),
                    onChanged: (v) => setState(() => _selectedSubId = v),
                  ),
                ),
              ],
            ),
          ),
          const Gap(16),
        ],

        SizedBox(
          width: double.infinity,
          height: 56,
          child: ElevatedButton(
            onPressed: _loading ? null : _createTunnel,
            style: ElevatedButton.styleFrom(
              backgroundColor: AppTheme.azure,
              foregroundColor: Colors.white,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
              elevation: 0,
            ),
            child: const Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(Icons.cloud_upload_rounded, size: 22),
                SizedBox(width: 12),
                Text('Create Tunnel', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16)),
              ],
            ),
          ),
        ),
        const Gap(16),
      ],
    ).animate().fadeIn(duration: 500.ms);
  }

  // -- Creating VM progress --

  Widget _buildCreatingCard() {
    return Column(
      children: [
        const Gap(40),
        const SizedBox(width: 60, height: 60, child: CircularProgressIndicator(color: AppTheme.azure, strokeWidth: 3)),
        const Gap(24),
        const Text('Creating Your Tunnel', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w700)),
        const Gap(12),
        Text(_progressMsg, style: TextStyle(color: AppTheme.primary.withAlpha(180), fontSize: 14)),
        const Gap(8),
        Text('This takes about 2 minutes...', style: TextStyle(color: Colors.white.withAlpha(80), fontSize: 12)),
        const Gap(40),
      ],
    ).animate().fadeIn(duration: 400.ms);
  }

  // -- VM deallocated --

  Widget _buildDeallocatedCard() {
    return Column(
      children: [
        const Gap(24),
        Icon(Icons.pause_circle_outline, size: 64, color: AppTheme.warning.withAlpha(150)),
        const Gap(16),
        const Text('VM Stopped', style: TextStyle(color: AppTheme.warning, fontSize: 18, fontWeight: FontWeight.w700)),
        const Gap(8),
        Text('No charges while stopped', style: TextStyle(color: Colors.white.withAlpha(100), fontSize: 13)),
        const Gap(24),
        Row(
          children: [
            Expanded(
              child: SizedBox(
                height: 50,
                child: ElevatedButton.icon(
                  onPressed: _loading ? null : _startVm,
                  icon: const Icon(Icons.play_arrow_rounded, size: 20),
                  label: const Text('Start VM', style: TextStyle(fontWeight: FontWeight.w700)),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: AppTheme.success,
                    foregroundColor: Colors.white,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                ),
              ),
            ),
            const Gap(12),
            SizedBox(
              height: 50,
              child: OutlinedButton(
                onPressed: _loading ? null : _deleteAll,
                style: OutlinedButton.styleFrom(
                  side: BorderSide(color: AppTheme.danger.withAlpha(120)),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                ),
                child: const Icon(Icons.delete_outline, color: AppTheme.danger, size: 20),
              ),
            ),
          ],
        ),
        if (_loading) ...[
          const Gap(16),
          const LinearProgressIndicator(color: AppTheme.success),
        ],
        const Gap(16),
      ],
    ).animate().fadeIn(duration: 500.ms);
  }

  // -- VM running, not connected --

  Widget _buildReadyCard(double usagePercent) {
    return Column(
      children: [
        const Gap(16),
        _buildPowerButton(usagePercent),
        const Gap(20),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.white.withAlpha(10),
            borderRadius: BorderRadius.circular(30),
            border: Border.all(color: Colors.white.withAlpha(30)),
          ),
          child: const Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.touch_app_rounded, color: Colors.white38, size: 16),
              SizedBox(width: 8),
              Text('Tap to connect', style: TextStyle(color: Colors.white38, fontSize: 13, fontWeight: FontWeight.w600)),
            ],
          ),
        ),
        const Gap(12),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            TextButton.icon(
              onPressed: _loading ? null : _deallocateVm,
              icon: Icon(Icons.pause_rounded, size: 16, color: AppTheme.warning.withAlpha(180)),
              label: Text('Stop VM', style: TextStyle(color: AppTheme.warning.withAlpha(180), fontSize: 12)),
            ),
            TextButton.icon(
              onPressed: _loading ? null : _deleteAll,
              icon: Icon(Icons.delete_outline, size: 16, color: AppTheme.danger.withAlpha(150)),
              label: Text('Delete', style: TextStyle(color: AppTheme.danger.withAlpha(150), fontSize: 12)),
            ),
          ],
        ),
      ],
    ).animate().fadeIn(duration: 500.ms);
  }

  // -- Connected --

  Widget _buildConnectedCard(double usagePercent, bool inWindow) {
    return Column(
      children: [
        const Gap(16),
        _buildPowerButton(usagePercent),
        const Gap(20),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
          decoration: BoxDecoration(
            color: AppTheme.success.withAlpha(20),
            borderRadius: BorderRadius.circular(30),
            border: Border.all(color: AppTheme.success.withAlpha(60)),
          ),
          child: const Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.verified_user_rounded, color: AppTheme.success, size: 16),
              SizedBox(width: 8),
              Text('Protected via Azure', style: TextStyle(color: AppTheme.success, fontSize: 13, fontWeight: FontWeight.w600)),
            ],
          ),
        ),
      ],
    ).animate().fadeIn(duration: 500.ms);
  }

  Widget _buildPowerButton(double usagePercent) {
    return GestureDetector(
      onTap: _loading ? null : () => _connected ? _disconnectVpn() : _connectVpn(),
      child: AnimatedBuilder(
        animation: _pulseCtrl,
        builder: (context, child) {
          final glowOpacity = _connected ? 0.15 + _pulseCtrl.value * 0.2 : 0.0;
          final glowRadius = _connected ? 40.0 + _pulseCtrl.value * 30.0 : 0.0;

          return SizedBox(
            width: 200,
            height: 200,
            child: Stack(
              alignment: Alignment.center,
              children: [
                SizedBox(
                  width: 190,
                  height: 190,
                  child: CustomPaint(painter: _UsageRingPainter(progress: usagePercent, connected: _connected)),
                ),
                if (_connected)
                  Container(
                    width: 145,
                    height: 145,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      boxShadow: [
                        BoxShadow(color: AppTheme.primary.withAlpha((glowOpacity * 255).toInt()), blurRadius: glowRadius, spreadRadius: 4),
                      ],
                    ),
                  ),
                Container(
                  width: 140,
                  height: 140,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: RadialGradient(
                      colors: _connected
                          ? [AppTheme.primary.withAlpha(50), AppTheme.surfaceCard]
                          : [AppTheme.surfaceCard, AppTheme.surface],
                    ),
                    border: Border.all(
                      color: _connected ? AppTheme.primary.withAlpha(120) : Colors.white.withAlpha(20),
                      width: 2,
                    ),
                  ),
                  child: _loading
                      ? const Center(child: CircularProgressIndicator(color: AppTheme.primary, strokeWidth: 2.5))
                      : Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Icon(
                              Icons.power_settings_new_rounded,
                              size: 44,
                              color: _connected ? AppTheme.primary : Colors.white.withAlpha(50),
                            ),
                            const Gap(6),
                            Text(
                              _connected ? 'CONNECTED' : 'CONNECT',
                              style: TextStyle(
                                color: _connected ? AppTheme.primary : Colors.white.withAlpha(80),
                                fontSize: 11,
                                fontWeight: FontWeight.w800,
                                letterSpacing: 2,
                              ),
                            ),
                          ],
                        ),
                ),
              ],
            ),
          );
        },
      ),
    ).animate().scale(duration: 600.ms, curve: Curves.elasticOut);
  }

  Widget _buildUsageCard(double usagePercent) {
    return _glassCard(
      child: Column(
        children: [
          Row(
            children: [
              const Icon(Icons.data_usage_rounded, color: AppTheme.primary, size: 18),
              const SizedBox(width: 8),
              const Text('Data Usage', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
              const Spacer(),
              Text(
                '${_usedGb.toStringAsFixed(2)} / ${_totalGb.toStringAsFixed(0)} GB',
                style: TextStyle(
                  color: usagePercent > 0.9 ? AppTheme.danger : AppTheme.primary,
                  fontWeight: FontWeight.w700,
                  fontSize: 14,
                ),
              ),
            ],
          ),
          const Gap(14),
          ClipRRect(
            borderRadius: BorderRadius.circular(6),
            child: LinearProgressIndicator(
              value: usagePercent,
              minHeight: 8,
              backgroundColor: Colors.white.withAlpha(15),
              valueColor: AlwaysStoppedAnimation<Color>(
                usagePercent > 0.9 ? AppTheme.danger : usagePercent > 0.7 ? AppTheme.warning : AppTheme.primary,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildTimeCard(bool inWindow) {
    return _glassCard(
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: (inWindow ? AppTheme.success : AppTheme.warning).withAlpha(25),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Icon(Icons.access_time_rounded, color: inWindow ? AppTheme.success : AppTheme.warning, size: 22),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  inWindow ? 'Bundle Active' : 'Bundle Inactive',
                  style: TextStyle(color: inWindow ? AppTheme.success : AppTheme.warning, fontWeight: FontWeight.w700, fontSize: 14),
                ),
                const Gap(2),
                Text('7:30 AM – 2:00 PM', style: TextStyle(color: Colors.white.withAlpha(100), fontSize: 12)),
              ],
            ),
          ),
          if (inWindow)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              decoration: BoxDecoration(color: AppTheme.success.withAlpha(25), borderRadius: BorderRadius.circular(20)),
              child: Text(_timeRemaining(), style: const TextStyle(color: AppTheme.success, fontWeight: FontWeight.w700, fontSize: 12)),
            ),
        ],
      ),
    );
  }

  Widget _buildVmInfoCard() {
    return _glassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.dns_rounded, color: AppTheme.azure, size: 18),
              const SizedBox(width: 8),
              const Text('Azure VM', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
              const Spacer(),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                decoration: BoxDecoration(
                  color: (_vm.isRunning ? AppTheme.success : AppTheme.warning).withAlpha(20),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Text(
                  _vm.status,
                  style: TextStyle(
                    color: _vm.isRunning ? AppTheme.success : AppTheme.warning,
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
          if (_vm.ip.isNotEmpty) ...[
            const Gap(10),
            Row(
              children: [
                Text('IP: ', style: TextStyle(color: Colors.white.withAlpha(100), fontSize: 12)),
                Text(_vm.ip, style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600, fontFamily: 'monospace')),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _glassCard({required Widget child}) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: AppTheme.surfaceCard.withAlpha(180),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: Colors.white.withAlpha(12)),
        boxShadow: [BoxShadow(color: Colors.black.withAlpha(40), blurRadius: 20, offset: const Offset(0, 4))],
      ),
      child: child,
    );
  }
}

class _UsageRingPainter extends CustomPainter {
  final double progress;
  final bool connected;

  _UsageRingPainter({required this.progress, required this.connected});

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = size.width / 2 - 8;
    const strokeWidth = 6.0;

    canvas.drawCircle(
      center,
      radius,
      Paint()
        ..color = Colors.white.withAlpha(15)
        ..style = PaintingStyle.stroke
        ..strokeWidth = strokeWidth
        ..strokeCap = StrokeCap.round,
    );

    if (progress > 0) {
      final paint = Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = strokeWidth
        ..strokeCap = StrokeCap.round
        ..shader = SweepGradient(
          startAngle: -pi / 2,
          endAngle: 3 * pi / 2,
          colors: [
            AppTheme.primary,
            progress > 0.7 ? AppTheme.warning : AppTheme.azure,
            progress > 0.9 ? AppTheme.danger : AppTheme.primary,
          ],
          stops: const [0.0, 0.5, 1.0],
        ).createShader(Rect.fromCircle(center: center, radius: radius));

      canvas.drawArc(
        Rect.fromCircle(center: center, radius: radius),
        -pi / 2,
        2 * pi * progress,
        false,
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _UsageRingPainter old) =>
      old.progress != progress || old.connected != connected;
}
