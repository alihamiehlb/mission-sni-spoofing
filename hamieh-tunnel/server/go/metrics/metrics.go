// Package metrics provides thread-safe counters and a Prometheus-compatible
// HTTP handler for the relay server.
package metrics

import (
	"fmt"
	"net/http"
	"sync/atomic"
	"time"
)

// Registry holds all relay metrics.
type Registry struct {
	startTime time.Time

	connActive int64 // atomic
	connTotal  int64 // atomic
	connClosed int64 // atomic

	bytesSent int64 // atomic
	bytesRecv int64 // atomic
}

// New creates a new Registry with start time set to now.
func New() *Registry {
	return &Registry{startTime: time.Now()}
}

func (r *Registry) ConnOpen()       { atomic.AddInt64(&r.connActive, 1); atomic.AddInt64(&r.connTotal, 1) }
func (r *Registry) ConnClose()      { atomic.AddInt64(&r.connActive, -1); atomic.AddInt64(&r.connClosed, 1) }
func (r *Registry) AddSent(n int64) { atomic.AddInt64(&r.bytesSent, n) }
func (r *Registry) AddRecv(n int64) { atomic.AddInt64(&r.bytesRecv, n) }

// Summary returns a JSON-serialisable map of current metrics.
func (r *Registry) Summary() map[string]interface{} {
	uptimeSec := time.Since(r.startTime).Seconds()
	return map[string]interface{}{
		"uptime_s": fmt.Sprintf("%.1f", uptimeSec),
		"connections": map[string]int64{
			"active": atomic.LoadInt64(&r.connActive),
			"total":  atomic.LoadInt64(&r.connTotal),
			"closed": atomic.LoadInt64(&r.connClosed),
		},
		"bandwidth": map[string]float64{
			"sent_mb": float64(atomic.LoadInt64(&r.bytesSent)) / (1024 * 1024),
			"recv_mb": float64(atomic.LoadInt64(&r.bytesRecv)) / (1024 * 1024),
		},
	}
}

// PrometheusHandler writes Prometheus-format metrics to w.
func (r *Registry) PrometheusHandler(w http.ResponseWriter, req *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	uptime := time.Since(r.startTime).Seconds()

	fmt.Fprintf(w, `# HELP hamieh_uptime_seconds Relay uptime
# TYPE hamieh_uptime_seconds gauge
hamieh_uptime_seconds %.1f

# HELP hamieh_connections_active Active connections
# TYPE hamieh_connections_active gauge
hamieh_connections_active %d

# HELP hamieh_connections_total Total connections opened
# TYPE hamieh_connections_total counter
hamieh_connections_total %d

# HELP hamieh_bytes_sent_total Bytes relayed to clients
# TYPE hamieh_bytes_sent_total counter
hamieh_bytes_sent_total %d

# HELP hamieh_bytes_recv_total Bytes received from clients
# TYPE hamieh_bytes_recv_total counter
hamieh_bytes_recv_total %d
`,
		uptime,
		atomic.LoadInt64(&r.connActive),
		atomic.LoadInt64(&r.connTotal),
		atomic.LoadInt64(&r.bytesSent),
		atomic.LoadInt64(&r.bytesRecv),
	)
}
