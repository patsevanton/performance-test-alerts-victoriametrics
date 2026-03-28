package main

import (
	"fmt"
	"math/rand"
	"net/http"
	"runtime"
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

	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			saturationGauge.Set(float64(runtime.NumGoroutine()))
		}
	}()

	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		client := &http.Client{
			Timeout: 1 * time.Second,
		}
		for range ticker.C {
			go func() {
				resp, err := client.Get("http://localhost:8080/work")
				if err == nil && resp != nil {
					resp.Body.Close()
				}
			}()
		}
	}()

	http.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
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

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "golden signal demo\n")
	})

	http.Handle("/metrics", promhttp.Handler())

	fmt.Println("serving on :8080")
	if err := http.ListenAndServe(":8080", nil); err != nil {
		panic(err)
	}
}
