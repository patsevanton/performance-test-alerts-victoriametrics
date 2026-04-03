package main

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	requestLatency = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "app_request_latency_seconds",
		Help:    "Время обработки HTTP-запроса",
		Buckets: prometheus.DefBuckets,
	})

	requestCount = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "app_requests_total",
		Help: "Всего входящих HTTP-запросов",
	})

	errorCount = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "app_errors_total",
		Help: "Число ответов с ошибкой",
	})

	saturationGauge = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "app_goroutines",
		Help: "Число горутин для контроля насыщения",
	})
)

func init() {
	prometheus.MustRegister(requestLatency, requestCount, errorCount, saturationGauge)
}

func main() {
	rand.Seed(time.Now().UnixNano())

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

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

	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		requestCount.Inc()

		time.Sleep(time.Duration(100+rand.Intn(400)) * time.Millisecond)
		latency := time.Since(start).Seconds()
		requestLatency.Observe(latency)

		if rand.Float64() < 0.2 {
			errorCount.Inc()
			http.Error(w, "internal failure", http.StatusInternalServerError)
			return
		}

		fmt.Fprintf(w, "processed in %.3f s\n", latency)
	})

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "golden signal demo\n")
	})

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "ok\n")
	})

	mux.Handle("/metrics", promhttp.Handler())

	server := &http.Server{
		Addr:    ":8080",
		Handler: mux,
	}

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
