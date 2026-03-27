// Hamieh Tunnel — Go Relay Server
//
// High-performance relay designed for mobile networks:
//   - Goroutine-per-connection (no async overhead)
//   - Zero-allocation hot paths with sync.Pool buffers
//   - TCP_NODELAY + SO_KEEPALIVE for mobile network resilience
//   - WebSocket/TLS + HTTPS CONNECT dual transport
//   - JWT token authentication
//   - Per-IP token-bucket rate limiting
//   - Prometheus metrics endpoint
//
// Build:
//   go build -o hamieh-relay .
//
// Run:
//   ./hamieh-relay --config /etc/hamieh/server.yaml
//   ./hamieh-relay --port 8443 --token mysecret --cert cert.pem --key key.pem

package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hamieh/hamieh-tunnel/auth"
	"github.com/hamieh/hamieh-tunnel/metrics"
	"github.com/hamieh/hamieh-tunnel/ratelimit"
	"github.com/hamieh/hamieh-tunnel/relay"
)

// Config holds all server configuration.
type Config struct {
	Host               string        `json:"host"`
	Port               int           `json:"port"`
	CertFile           string        `json:"cert_file"`
	KeyFile            string        `json:"key_file"`
	Token              string        `json:"token"`
	MaxClients         int           `json:"max_clients"`
	ConnectTimeout     time.Duration `json:"connect_timeout"`
	ReqPerMinute       int           `json:"requests_per_minute"`
	BandwidthBytesPerS int64         `json:"bandwidth_bytes_per_second"`
	BanThreshold       int           `json:"ban_threshold"`
	MetricsPort        int           `json:"metrics_port"`
	LogLevel           string        `json:"log_level"`
}

func defaultConfig() *Config {
	return &Config{
		Host:               "0.0.0.0",
		Port:               8443,
		CertFile:           "certs/relay_cert.pem",
		KeyFile:            "certs/relay_key.pem",
		Token:              "",
		MaxClients:         1000,
		ConnectTimeout:     15 * time.Second,
		ReqPerMinute:       600,
		BandwidthBytesPerS: 10 * 1024 * 1024, // 10 MB/s
		BanThreshold:       10,
		MetricsPort:        9100,
		LogLevel:           "info",
	}
}

func loadConfig(path string) (*Config, error) {
	cfg := defaultConfig()
	if path == "" {
		return cfg, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config: %w", err)
	}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	return cfg, nil
}

func main() {
	var (
		configPath = flag.String("config", "", "JSON config file")
		port       = flag.Int("port", 0, "Override relay port")
		token      = flag.String("token", "", "Override auth token")
		certFile   = flag.String("cert", "", "Override TLS cert path")
		keyFile    = flag.String("key", "", "Override TLS key path")
	)
	flag.Parse()

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("Config error: %v", err)
	}

	// CLI flag overrides
	if *port != 0 {
		cfg.Port = *port
	}
	if *token != "" {
		cfg.Token = *token
	}
	if *certFile != "" {
		cfg.CertFile = *certFile
	}
	if *keyFile != "" {
		cfg.KeyFile = *keyFile
	}

	// Environment variable overrides
	if t := os.Getenv("HAMIEH_AUTH_TOKEN"); t != "" {
		cfg.Token = t
	}

	if cfg.Token == "" {
		log.Fatal("Auth token is required. Set --token or HAMIEH_AUTH_TOKEN")
	}

	// Build TLS config optimised for mobile networks
	tlsCfg, err := buildTLSConfig(cfg)
	if err != nil {
		log.Fatalf("TLS setup failed: %v", err)
	}

	// Initialise subsystems
	m := metrics.New()
	rl := ratelimit.New(cfg.ReqPerMinute, cfg.BandwidthBytesPerS, cfg.BanThreshold)
	authenticator := auth.New(cfg.Token)

	srv := relay.NewServer(relay.Options{
		Host:           cfg.Host,
		Port:           cfg.Port,
		TLSConfig:      tlsCfg,
		ConnectTimeout: cfg.ConnectTimeout,
		MaxClients:     cfg.MaxClients,
		Auth:           authenticator,
		RateLimiter:    rl,
		Metrics:        m,
	})

	// Start metrics HTTP server
	go func() {
		addr := fmt.Sprintf("127.0.0.1:%d", cfg.MetricsPort)
		log.Printf("[metrics] Prometheus endpoint: http://%s/metrics", addr)
		mux := http.NewServeMux()
		mux.HandleFunc("/metrics", m.PrometheusHandler)
		mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
			w.Write([]byte("ok"))
		})
		mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(m.Summary())
		})
		if err := http.ListenAndServe(addr, mux); err != nil {
			log.Printf("[metrics] server error: %v", err)
		}
	}()

	// Graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		log.Printf("[main] received %v — shutting down", sig)
		cancel()
	}()

	log.Printf("[main] Hamieh Relay starting on %s:%d", cfg.Host, cfg.Port)
	log.Printf("[main] HTTPS CONNECT on %s:%d", cfg.Host, cfg.Port+1)
	log.Printf("[main] Raw TLS (phones) on %s:%d", cfg.Host, cfg.Port+2)

	if err := srv.Start(ctx); err != nil {
		log.Fatalf("Server error: %v", err)
	}

	log.Printf("[main] Shutdown complete. %s", m.Summary())
}

// buildTLSConfig returns a TLS config tuned for mobile-network performance:
//   - TLS 1.3 preferred (faster handshake, 0-RTT capable)
//   - Session tickets enabled for resumption (avoids full handshake on reconnect)
//   - Optimised cipher suites
func buildTLSConfig(cfg *Config) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
	if err != nil {
		// Try to auto-generate if missing
		if err2 := generateSelfSignedCert(cfg.CertFile, cfg.KeyFile); err2 != nil {
			return nil, fmt.Errorf("load TLS cert (%v) and auto-gen failed (%v)", err, err2)
		}
		cert, err = tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
		if err != nil {
			return nil, err
		}
	}

	return &tls.Config{
		Certificates: []tls.Certificate{cert},
		MinVersion:   tls.VersionTLS12,
		// Prefer TLS 1.3: single round-trip handshake (vs 2 for TLS 1.2)
		// TLS 1.3 also enables 0-RTT session resumption which is critical for
		// mobile clients that frequently reconnect after going to background
		PreferServerCipherSuites: false, // TLS 1.3 handles this automatically
		CipherSuites: []uint16{
			// TLS 1.2 fallback ciphers — ordered by performance on mobile ARM CPUs
			tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
			tls.TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
			tls.TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256, // best on mobile (no AES-NI)
			tls.TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
			tls.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384,
			tls.TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,
		},
		// Session ticket keys for TLS resumption — reduces handshake to ~1 RTT
		SessionTicketsDisabled: false,
	}, nil
}
