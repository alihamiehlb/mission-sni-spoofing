package main

import (
	"bufio"
	"crypto/tls"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

var (
	listenAddr = flag.String("listen", "127.0.0.1:1080", "SOCKS5 listen address")
	relayHost  = flag.String("relay", "", "Relay server host")
	relayPort  = flag.Int("port", 9443, "Relay server port")
	sni        = flag.String("sni", "teams.microsoft.com", "SNI to spoof")
	token      = flag.String("token", "", "Relay auth token")
	configFile = flag.String("config", "", "Config file path")
)

var bufPool = sync.Pool{
	New: func() interface{} {
		buf := make([]byte, 32*1024)
		return &buf
	},
}

func main() {
	flag.Parse()

	if *configFile != "" {
		loadConfig(*configFile)
	}

	if *relayHost == "" {
		log.Fatal("--relay is required (relay server IP/hostname)")
	}

	log.Printf("Hash-Sec Client | SOCKS5=%s | Relay=%s:%d | SNI=%s",
		*listenAddr, *relayHost, *relayPort, *sni)

	ln, err := net.Listen("tcp", *listenAddr)
	if err != nil {
		log.Fatalf("Listen failed: %v", err)
	}
	defer ln.Close()

	for {
		conn, err := ln.Accept()
		if err != nil {
			continue
		}
		go handleClient(conn)
	}
}

func handleClient(conn net.Conn) {
	defer conn.Close()

	if err := socks5Handshake(conn); err != nil {
		return
	}

	dstHost, dstPort, err := socks5ReadRequest(conn)
	if err != nil {
		return
	}

	relayConn, err := connectRelay(dstHost, dstPort)
	if err != nil {
		socks5Reply(conn, 0x05)
		return
	}
	defer relayConn.Close()

	socks5Reply(conn, 0x00)
	biRelay(conn, relayConn)
}

func connectRelay(dstHost string, dstPort int) (net.Conn, error) {
	cfg := &tls.Config{
		ServerName:         *sni,
		InsecureSkipVerify: true,
		MinVersion:         tls.VersionTLS12,
		CipherSuites: []uint16{
			tls.TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
			tls.TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
			tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
			tls.TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
		},
	}

	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	conn, err := tls.DialWithDialer(dialer, "tcp",
		fmt.Sprintf("%s:%d", *relayHost, *relayPort), cfg)
	if err != nil {
		return nil, err
	}

	// Relay protocol: [1B token_len][token][1B host_len][host][2B port BE]
	var hdr []byte
	tk := []byte(*token)
	hdr = append(hdr, byte(len(tk)))
	if len(tk) > 0 {
		hdr = append(hdr, tk...)
	}
	host := []byte(dstHost)
	hdr = append(hdr, byte(len(host)))
	hdr = append(hdr, host...)
	p := make([]byte, 2)
	binary.BigEndian.PutUint16(p, uint16(dstPort))
	hdr = append(hdr, p...)

	if _, err := conn.Write(hdr); err != nil {
		conn.Close()
		return nil, err
	}

	status := make([]byte, 1)
	if _, err := io.ReadFull(conn, status); err != nil {
		conn.Close()
		return nil, err
	}
	if status[0] != 0x00 {
		conn.Close()
		return nil, fmt.Errorf("relay refused: 0x%02x", status[0])
	}
	return conn, nil
}

func socks5Handshake(conn net.Conn) error {
	buf := make([]byte, 258)
	if _, err := io.ReadFull(conn, buf[:2]); err != nil {
		return err
	}
	if buf[0] != 0x05 {
		return fmt.Errorf("not SOCKS5")
	}
	if _, err := io.ReadFull(conn, buf[:buf[1]]); err != nil {
		return err
	}
	_, err := conn.Write([]byte{0x05, 0x00})
	return err
}

func socks5ReadRequest(conn net.Conn) (string, int, error) {
	buf := make([]byte, 4)
	if _, err := io.ReadFull(conn, buf); err != nil {
		return "", 0, err
	}
	if buf[1] != 0x01 {
		socks5Reply(conn, 0x07)
		return "", 0, fmt.Errorf("unsupported cmd")
	}

	var host string
	switch buf[3] {
	case 0x01:
		a := make([]byte, 4)
		io.ReadFull(conn, a)
		host = net.IP(a).String()
	case 0x03:
		l := make([]byte, 1)
		io.ReadFull(conn, l)
		d := make([]byte, l[0])
		io.ReadFull(conn, d)
		host = string(d)
	case 0x04:
		a := make([]byte, 16)
		io.ReadFull(conn, a)
		host = net.IP(a).String()
	default:
		socks5Reply(conn, 0x08)
		return "", 0, fmt.Errorf("bad atyp")
	}

	pb := make([]byte, 2)
	io.ReadFull(conn, pb)
	return host, int(binary.BigEndian.Uint16(pb)), nil
}

func socks5Reply(conn net.Conn, status byte) {
	conn.Write([]byte{0x05, status, 0x00, 0x01, 0, 0, 0, 0, 0, 0})
}

func biRelay(a, b net.Conn) {
	done := make(chan struct{}, 2)

	cp := func(dst, src net.Conn) {
		bufPtr := bufPool.Get().(*[]byte)
		io.CopyBuffer(dst, src, *bufPtr)
		bufPool.Put(bufPtr)
		done <- struct{}{}
	}

	go cp(b, a)
	go cp(a, b)
	<-done
}

func loadConfig(path string) {
	f, err := os.Open(path)
	if err != nil {
		log.Printf("Config warning: %v", err)
		return
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		k := strings.TrimSpace(parts[0])
		v := strings.TrimSpace(strings.Trim(parts[1], "\"'"))
		switch k {
		case "relay_host":
			*relayHost = v
		case "relay_port":
			fmt.Sscanf(v, "%d", relayPort)
		case "sni":
			*sni = v
		case "token", "password":
			*token = v
		case "bind_host":
			_, port := splitHostPort(*listenAddr)
			*listenAddr = v + ":" + port
		case "bind_port":
			host, _ := splitHostPort(*listenAddr)
			*listenAddr = host + ":" + v
		}
	}
}

func splitHostPort(addr string) (string, string) {
	h, p, err := net.SplitHostPort(addr)
	if err != nil {
		return "127.0.0.1", "1080"
	}
	return h, p
}
