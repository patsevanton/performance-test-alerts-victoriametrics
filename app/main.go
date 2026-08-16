package main

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Лейбл-пространства по PLAN-high-cardinality.md (1.1).
var (
	methods     = []string{"GET", "POST", "PUT", "DELETE", "PATCH"}
	endpoints   = []string{"/work", "/healthz", "/metrics", "/api/v1", "/api/v2"}
	statusCodes = []string{"200", "429", "500", "503", "504"}
)

// envInt читает целое из env с дефолтом.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// makeRoutes формирует route-0 .. route-(N-1).
func makeRoutes(n int) []string {
	r := make([]string, n)
	for i := 0; i < n; i++ {
		r[i] = fmt.Sprintf("route-%d", i)
	}
	return r
}

// makeTenants формирует tenant-0 .. tenant-(M-1).
func makeTenants(n int) []string {
	t := make([]string, n)
	for i := 0; i < n; i++ {
		t[i] = fmt.Sprintf("tenant-%d", i)
	}
	return t
}

// pick выбирает случайный элемент слайса.
func pick[T any](r *rand.Rand, s []T) T { return s[r.Intn(len(s))] }

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Параметризация кардинальности через env (PLAN 1.1, 3).
	tenants := makeTenants(envInt("APP_TENANTS", 50))
	routes := makeRoutes(envInt("APP_ROUTES", 10))
	histBuckets := envInt("APP_HIST_BUCKETS", 50)
	region := envStr("APP_REGION", "ru-central1-b")
	version := envStr("APP_VERSION", "1.7.0")

	labelNames := []string{"method", "endpoint", "status_code", "route", "tenant_id", "region", "version"}

	buckets := prometheus.ExponentialBuckets(0.005, 1.15, histBuckets)

	requestLatency := prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "app_request_latency_seconds",
			Help:    "Время обработки HTTP-запроса",
			Buckets: buckets,
		},
		labelNames,
	)

	requestDuration := prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "app_request_duration_seconds",
			Help:    "Длительность HTTP-запроса (альтернатива с большим числом бакетов)",
			Buckets: buckets,
		},
		labelNames,
	)

	requestCount := prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "app_requests_total",
			Help: "Всего входящих HTTP-запросов",
		},
		labelNames,
	)

	errorCount := prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "app_errors_total",
			Help: "Число ответов с ошибкой",
		},
		labelNames,
	)

	saturationGauge := prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "app_goroutines",
		Help: "Число горутин для контроля насыщения",
	})

	inflightRequests := prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "app_inflight_requests",
			Help: "Число запросов в обработке",
		},
		[]string{"route", "tenant_id"},
	)

	cacheOperations := prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "app_cache_operations_total",
			Help: "Операции с кэшем (hit/miss)",
		},
		[]string{"cache_hit", "route", "tenant_id"},
	)

	queueSize := prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "app_queue_size",
			Help: "Размер очереди по tenant",
		},
		[]string{"queue", "tenant_id"},
	)

	prometheus.MustRegister(
		requestLatency, requestDuration, requestCount, errorCount,
		saturationGauge, inflightRequests, cacheOperations, queueSize,
	)

	// Детерминированный источник случайности (достаточен для генератора нагрузки).
	rnd := rand.New(rand.NewSource(time.Now().UnixNano()))

	// pickStatus возвращает статус-код согласно вероятностям PLAN 1.4:
	// 200 — 65%, 429 — 5%, 500 — 20%, 503 — 5%, 504 — 5%.
	// Веса выровнены по порядку элементов слайса statusCodes.
	pickStatus := func() string {
		weights := []float64{0.65, 0.05, 0.20, 0.05, 0.05}
		x := rnd.Float64()
		var acc float64
		for i, w := range weights {
			acc += w
			if x < acc {
				return statusCodes[i]
			}
		}
		return statusCodes[0]
	}

	// observeRequest инкрементит все Vec-метрики с полным набором лейблов.
	observeRequest := func(method, endpoint, status, route, tenant string, latency float64) {
		lbls := prometheus.Labels{
			"method":      method,
			"endpoint":    endpoint,
			"status_code": status,
			"route":       route,
			"tenant_id":   tenant,
			"region":      region,
			"version":     version,
		}
		requestCount.With(lbls).Inc()
		requestLatency.With(lbls).Observe(latency)
		requestDuration.With(lbls).Observe(latency)
		if status != "200" {
			errorCount.With(lbls).Inc()
		}
	}

	// handleWork — обработчик /work (PLAN 1.3).
	handleWork := func(w http.ResponseWriter, r *http.Request) {
		method := r.Method
		if !contains(methods, method) {
			method = pick(rnd, methods)
		}
		route := pick(rnd, routes)
		tenant := pick(rnd, tenants)
		status := pickStatus()

		inflight := inflightRequests.With(prometheus.Labels{"route": route, "tenant_id": tenant})
		inflight.Inc()
		defer inflight.Dec()

		start := time.Now()
		time.Sleep(time.Duration(100+rnd.Intn(400)) * time.Millisecond)
		latency := time.Since(start).Seconds()

		// Эмуляция cache hit/miss.
		cacheHit := "miss"
		if rnd.Float64() < 0.6 {
			cacheHit = "hit"
		}
		cacheOperations.With(prometheus.Labels{
			"cache_hit":  cacheHit,
			"route":      route,
			"tenant_id": tenant,
		}).Inc()

		// Эмуляция очереди по tenant.
		queue := "q0"
		queueSize.With(prometheus.Labels{"queue": queue, "tenant_id": tenant}).Set(float64(rnd.Intn(20)))

		// Эмуляция метрики по случайно выбранному endpoint из слайса endpoints,
		// чтобы разнообразить лейбл endpoint (а не только "/work").
		endpoint := pick(rnd, endpoints)

		observeRequest(method, endpoint, status, route, tenant, latency)

		if status != "200" {
			http.Error(w, "internal failure", httpStatus(status))
			return
		}
		fmt.Fprintf(w, "processed in %.3f s\n", latency)
	}

	// Сборщик goroutines.
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				saturationGauge.Set(float64(runtime.NumGoroutine()))
			}
		}
	}()

	// Фоновый тикер: каждый запрос берёт случайные tenant/route/method (PLAN 1.4).
	client := &http.Client{Timeout: 1 * time.Second}
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				go func() {
					resp, err := client.Get("http://localhost:8080/work")
					if err == nil && resp != nil {
						resp.Body.Close()
					}
				}()
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/work", handleWork)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "golden signal demo\n")
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "ok\n")
	})
	mux.Handle("/metrics", promhttp.Handler())

	server := &http.Server{Addr: ":8080", Handler: mux}

	go func() {
		fmt.Println("serving on :8080")
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintf(os.Stderr, "listen error: %v\n", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	fmt.Println("shutting down gracefully...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	if err := server.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintf(os.Stderr, "shutdown error: %v\n", err)
	}
	fmt.Println("shutdown complete")
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func httpStatus(code string) int {
	switch code {
	case "429":
		return http.StatusTooManyRequests
	case "500":
		return http.StatusInternalServerError
	case "503":
		return http.StatusServiceUnavailable
	case "504":
		return http.StatusGatewayTimeout
	default:
		return http.StatusOK
	}
}
