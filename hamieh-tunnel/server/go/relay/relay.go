package relay

import (
	"context"
	"crypto/tls"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hamieh/hamieh-tunnel/auth"
	"github.com/hamieh/hamieh-tunnel/metrics"
	"github.com/hamieh/hamieh-tunnel/ratelimit"
	"nhooyr.io/websocket"
)

type Options struct {
	Host           string
	Port           int
	TLSConfig      *tls.Config
	ConnectTimeout time.Duration
	MaxClients     int
	Auth           *auth.Authenticator
	RateLimiter    *ratelimit.Limiter
	Metrics        *metrics.Registry
}

type Server struct {
	opts    Options
	active  atomic.Int64
	bufPool sync.Pool
}

func NewServer(opts Options) *Server {
	return &Server{
		opts: opts,
		bufPool: sync.Pool{
			New: func() interface{} {
				buf := make([]byte, 65536)
				return &buf
			},
		},
	}
}

func (s *Server) Start(ctx context.Context) error {
	wssErrCh := make(chan error, 1)
	go func() {
		addr := fmt.Sprintf("%s:%d", s.opts.Host, s.opts.Port)
		wssErrCh <- s.serveWSS(ctx, addr)
	}()

	httpsErrCh := make(chan error, 1)
	go func() {
		addr := fmt.Sprintf("%s:%d", s.opts.Host, s.opts.Port+1)
		httpsErrCh <- s.serveHTTPS(ctx, addr)
	}()

	rawErrCh := make(chan error, 1)
	go func() {
		addr := fmt.Sprintf("%s:%d", s.opts.Host, s.opts.Port+2)
		rawErrCh <- s.serveRawTLS(ctx, addr)
	}()

	select {
	case err := <-wssErrCh:
		return fmt.Errorf("WSS server: %w", err)
	case err := <-httpsErrCh:
		return fmt.Errorf("HTTPS server: %w", err)
	case err := <-rawErrCh:
		return fmt.Errorf("Raw TLS server: %w", err)
	case <-ctx.Done():
		return nil
	}
}

// Raw TLS mode for hamieh-client (phones).
// Protocol:
//   Client → Server: [1B token_len][token][1B host_len][host][2B port BE]
//   Server → Client: [1B status] (0x00=OK, 0x01=auth fail, 0x02=connect fail)
//   Then bidirectional relay.
func (s *Server) serveRawTLS(ctx context.Context, addr string) error {
	ln, err := tls.Listen("tcp", addr, s.opts.TLSConfig)
	if err != nil {
		return err
	}
	log.Printf("[raw-tls] Listening on %s (phone clients)", addr)

	go func() {
		<-ctx.Done()
		ln.Close()
	}()

	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
				continue
			}
		}
		go s.handleRawTLS(conn)
	}
}

func (s *Server) handleRawTLS(conn net.Conn) {
	defer conn.Close()

	clientIP := conn.RemoteAddr().String()
	if host, _, err := net.SplitHostPort(clientIP); err == nil {
		clientIP = host
	}

	if !s.opts.RateLimiter.Allow(clientIP) {
		conn.Write([]byte{0x03})
		return
	}

	current := s.active.Load()
	if current >= int64(s.opts.MaxClients) {
		conn.Write([]byte{0x04})
		return
	}

	conn.SetDeadline(time.Now().Add(15 * time.Second))

	// Read token
	tokenLenBuf := make([]byte, 1)
	if _, err := io.ReadFull(conn, tokenLenBuf); err != nil {
		return
	}
	tokenLen := int(tokenLenBuf[0])
	token := ""
	if tokenLen > 0 {
		tokenBuf := make([]byte, tokenLen)
		if _, err := io.ReadFull(conn, tokenBuf); err != nil {
			return
		}
		token = string(tokenBuf)
	}

	if !s.opts.Auth.Verify(token) {
		s.opts.RateLimiter.RecordAuthFailure(clientIP)
		conn.Write([]byte{0x01})
		return
	}
	s.opts.RateLimiter.ResetAuthFailures(clientIP)

	// Read destination
	hostLenBuf := make([]byte, 1)
	if _, err := io.ReadFull(conn, hostLenBuf); err != nil {
		return
	}
	hostBuf := make([]byte, hostLenBuf[0])
	if _, err := io.ReadFull(conn, hostBuf); err != nil {
		return
	}
	portBuf := make([]byte, 2)
	if _, err := io.ReadFull(conn, portBuf); err != nil {
		return
	}
	dstHost := string(hostBuf)
	dstPort := binary.BigEndian.Uint16(portBuf)

	log.Printf("[raw-tls] %s → %s:%d", clientIP, dstHost, dstPort)

	// Connect to real destination
	conn.SetDeadline(time.Time{})
	dst, err := net.DialTimeout("tcp",
		fmt.Sprintf("%s:%d", dstHost, dstPort), s.opts.ConnectTimeout)
	if err != nil {
		conn.Write([]byte{0x02})
		return
	}
	defer dst.Close()

	if tc, ok := dst.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
		tc.SetKeepAlive(true)
		tc.SetKeepAlivePeriod(30 * time.Second)
	}

	conn.Write([]byte{0x00})

	s.active.Add(1)
	s.opts.Metrics.ConnOpen()
	defer func() {
		s.active.Add(-1)
		s.opts.Metrics.ConnClose()
	}()

	// Bidirectional relay
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		n, _ := s.pooledCopy(dst, conn)
		s.opts.Metrics.AddRecv(n)
	}()
	go func() {
		defer wg.Done()
		n, _ := s.pooledCopy(conn, dst)
		s.opts.Metrics.AddSent(n)
	}()
	wg.Wait()
}

// WSS Server

func (s *Server) serveWSS(ctx context.Context, addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/tunnel", s.handleWSS)
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		TLSConfig:         s.opts.TLSConfig,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      0,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    1 << 16,
	}

	log.Printf("[wss] Listening on wss://%s/tunnel", addr)

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	return srv.ListenAndServeTLS("", "")
}

func (s *Server) handleWSS(w http.ResponseWriter, r *http.Request) {
	clientIP, _, _ := net.SplitHostPort(r.RemoteAddr)

	if !s.opts.RateLimiter.Allow(clientIP) {
		http.Error(w, "Rate limit exceeded", http.StatusTooManyRequests)
		return
	}

	current := s.active.Load()
	if current >= int64(s.opts.MaxClients) {
		http.Error(w, "Server at capacity", http.StatusServiceUnavailable)
		return
	}

	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		InsecureSkipVerify: true,
	})
	if err != nil {
		return
	}
	defer conn.CloseNow()

	s.active.Add(1)
	s.opts.Metrics.ConnOpen()
	defer func() {
		s.active.Add(-1)
		s.opts.Metrics.ConnClose()
	}()

	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	_, msg, err := conn.Read(ctx)
	if err != nil {
		return
	}

	dstHost, dstPort, token, err := parseOpenFrame(msg)
	if err != nil {
		conn.Write(r.Context(), websocket.MessageBinary, statusFrame(0x01))
		return
	}

	if !s.opts.Auth.Verify(token) {
		s.opts.RateLimiter.RecordAuthFailure(clientIP)
		conn.Write(r.Context(), websocket.MessageBinary, statusFrame(0x01))
		return
	}
	s.opts.RateLimiter.ResetAuthFailures(clientIP)

	dialCtx, dialCancel := context.WithTimeout(r.Context(), s.opts.ConnectTimeout)
	defer dialCancel()

	dst, err := (&net.Dialer{
		Timeout:   s.opts.ConnectTimeout,
		KeepAlive: 30 * time.Second,
	}).DialContext(dialCtx, "tcp", fmt.Sprintf("%s:%d", dstHost, dstPort))
	if err != nil {
		conn.Write(r.Context(), websocket.MessageBinary, statusFrame(0x02))
		return
	}
	defer dst.Close()

	if tc, ok := dst.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
		tc.SetKeepAlive(true)
		tc.SetKeepAlivePeriod(30 * time.Second)
	}

	cancel()
	relayCtx := r.Context()
	conn.Write(relayCtx, websocket.MessageBinary, statusFrame(0x00))

	m := s.opts.Metrics
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		defer dst.Close()
		for {
			_, data, err := conn.Read(relayCtx)
			if err != nil {
				return
			}
			if _, err := dst.Write(data); err != nil {
				return
			}
			m.AddRecv(int64(len(data)))
			s.opts.RateLimiter.AddBytes(clientIP, int64(len(data)))
		}
	}()

	go func() {
		defer wg.Done()
		bufPtr := s.bufPool.Get().(*[]byte)
		defer s.bufPool.Put(bufPtr)
		buf := *bufPtr
		for {
			n, err := dst.Read(buf)
			if n > 0 {
				if err2 := conn.Write(relayCtx, websocket.MessageBinary, buf[:n]); err2 != nil {
					return
				}
				m.AddSent(int64(n))
			}
			if err != nil {
				return
			}
		}
	}()

	wg.Wait()
}

// HTTPS CONNECT Server

func (s *Server) serveHTTPS(ctx context.Context, addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleHTTPSConnect)

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		TLSConfig:         s.opts.TLSConfig,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      0,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("[https] Listening on https://%s (CONNECT)", addr)

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdownCtx)
	}()

	return srv.ListenAndServeTLS("", "")
}

func (s *Server) handleHTTPSConnect(w http.ResponseWriter, r *http.Request) {
	clientIP, _, _ := net.SplitHostPort(r.RemoteAddr)

	if r.Method != http.MethodConnect {
		http.Error(w, "CONNECT required", http.StatusMethodNotAllowed)
		return
	}

	if !s.opts.RateLimiter.Allow(clientIP) {
		http.Error(w, "Rate limit exceeded", http.StatusTooManyRequests)
		return
	}

	authHeader := r.Header.Get("Proxy-Authorization")
	token := ""
	if len(authHeader) > 7 {
		token = authHeader[7:]
	}
	if !s.opts.Auth.Verify(token) {
		s.opts.RateLimiter.RecordAuthFailure(clientIP)
		http.Error(w, "Proxy Authentication Required", http.StatusProxyAuthRequired)
		return
	}

	dstAddr := r.Host
	dst, err := net.DialTimeout("tcp", dstAddr, s.opts.ConnectTimeout)
	if err != nil {
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}
	defer dst.Close()

	w.WriteHeader(http.StatusOK)

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		return
	}
	clientConn, _, err := hijacker.Hijack()
	if err != nil {
		return
	}
	defer clientConn.Close()

	s.active.Add(1)
	s.opts.Metrics.ConnOpen()
	defer func() {
		s.active.Add(-1)
		s.opts.Metrics.ConnClose()
	}()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		n, _ := s.pooledCopy(dst, clientConn)
		s.opts.Metrics.AddRecv(n)
	}()
	go func() {
		defer wg.Done()
		n, _ := s.pooledCopy(clientConn, dst)
		s.opts.Metrics.AddSent(n)
	}()
	wg.Wait()
}

// Helpers

func parseOpenFrame(data []byte) (host string, port uint16, token string, err error) {
	if len(data) < 4 {
		return "", 0, "", fmt.Errorf("frame too short")
	}
	if data[0] != 0x01 {
		return "", 0, "", fmt.Errorf("expected OPEN(0x01)")
	}
	hostLen := int(data[1])
	if len(data) < 2+hostLen+2+2 {
		return "", 0, "", fmt.Errorf("frame truncated")
	}
	host = string(data[2 : 2+hostLen])
	port = binary.BigEndian.Uint16(data[2+hostLen : 2+hostLen+2])
	tokenLen := int(binary.BigEndian.Uint16(data[2+hostLen+2 : 2+hostLen+4]))
	if tokenLen > 0 && len(data) >= 2+hostLen+4+tokenLen {
		token = string(data[2+hostLen+4 : 2+hostLen+4+tokenLen])
	}
	return host, port, token, nil
}

func statusFrame(status byte) []byte {
	return []byte{0x10, status}
}

func (s *Server) pooledCopy(dst io.Writer, src io.Reader) (int64, error) {
	bufPtr := s.bufPool.Get().(*[]byte)
	defer s.bufPool.Put(bufPtr)
	return io.CopyBuffer(dst, src, *bufPtr)
}
