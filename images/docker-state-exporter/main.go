package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/docker/docker/api/types"
	tcontainer "github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"github.com/go-kit/kit/log"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const (
	// cachePeriod indicates the period of time the collector will reuse the results of docker inspect.
	cachePeriod = 1 * time.Second

	// ── BOUNDED I/O BUDGETS ─ added 2026-08-10, homebase incident ──────────────
	// WHY THESE EXIST AT ALL: upstream passed context.Background() to every Docker
	// API call, i.e. NO deadline whatsoever. Go's http.Client has no default
	// timeout either, so a call that the daemon never answers hangs FOREVER — the
	// caller's only bound is whatever the intermediary imposes. In the homebase
	// that intermediary is tecnativa/docker-socket-proxy, whose haproxy.cfg
	// hardcodes `timeout server 10m` (VERIFIED against the v0.4.2 tag; it is NOT
	// tunable by env — there is no TIMEOUT_SERVER, only the API-section flags,
	// SOCKET_PATH and LOG_LEVEL). So a single unanswerable inspect cost this
	// process TEN MINUTES of wall clock before the proxy synthesised a 504.
	// STILL TRUE FOR INSPECTS after home-tech forked that config on 2026-08-11: the
	// fork widens ONE routing regex so /containers/{id}/logs reaches the streaming
	// backend, and deliberately leaves /containers/json + /containers/{id}/json on
	// the 10m backend — unbounding an inspect would restore exactly the hang these
	// budgets exist to bound.
	//
	// THE INCIDENT: onyx-background's container wedged at the daemon (its
	// /containers/<id>/json never returned — `docker logs` for it hung too, so the
	// wedge is in dockerd, not the proxy). Every 60s scrape walked the container
	// list, blocked 600s on that one container, took a 504, and — because the old
	// code called errCheck() on it — os.Exit(1)'d the whole exporter. Restart
	// policy brought it back, the next scrape hit the same container, and the
	// homebase lost EVERY docker_container_* series on an ~11 minute cycle for
	// hours. One sick container blinded the monitoring for all 33.
	//
	// Sizing against the real scrape config (home-tech scrape.yml, job
	// docker-health): scrape_interval 60s, scrape_timeout 55s. A healthy full
	// sweep of ~39 containers measures ~750ms end to end, so these budgets are
	// enormous headroom for the healthy path and only ever bind on a sick one:
	//   inspectTimeout — per container. One wedged container now costs 5s, not
	//     600s, and costs it ONCE per scrape instead of taking the process down.
	//   listTimeout    — the single /containers/json call that opens each sweep.
	//   collectBudget  — the ceiling for the WHOLE sweep, so N sick containers
	//     cannot multiply into N*inspectTimeout. Every per-call context descends
	//     from it: once it expires the remaining inspects fail instantly and are
	//     skipped, and the sweep returns with whatever it managed to collect.
	// collectBudget MUST stay comfortably under scrape_timeout — a sweep that
	// outlives the scrape is discarded by the scraper and the work is wasted.
	collectBudget  = 25 * time.Second
	listTimeout    = 10 * time.Second
	inspectTimeout = 5 * time.Second
)

type dockerHealthCollector struct {
	mu                 sync.Mutex
	containerClient    *client.Client
	containerInfoCache []types.ContainerJSON
	lastseen           time.Time
	// collectErrors is the number of containers skipped by the LAST sweep because
	// their inspect did not answer within inspectTimeout, or -1 when the container
	// list itself failed. Exported as container_state_collect_errors so a partial
	// collection is VISIBLE rather than silently short — the whole point of the fix
	// is that we now degrade instead of dying, and a degradation nobody can see is
	// its own trap.
	//
	// ⚠ IT COUNTS OUR GIVING UP, NOT A DAEMON FAULT — measured 2026-08-11 against
	// the homebase's proxy access log. EVERY deadline breach in a 12h window was
	// answered HTTP 200 by dockerd, after we had already abandoned the call: a 5s
	// deadline against inspects that took 6.2s / 10.7s / 17.1s / 21.1s. The
	// container's state was fully retrievable; it is dropped from that scrape's
	// series set anyway. So a non-zero value means "this container was slower than
	// our budget", and the escalation test is the RATE, not the value — ~1/hr with
	// never more than 1 per sweep is a SLOW container (in the homebase, traefik: see
	// home-tech services.d/docker-socket-proxy.yaml), while a failure on every
	// consecutive minute is a WEDGED one, the shape that caused the incident above.
	// Knock-on worth knowing when reading a sweep's timings: the inspects share a
	// keep-alive connection and run serially, so one slow container also delays the
	// rest of that sweep by seconds.
	collectErrors int
}

type descSource struct {
	name string
	help string
}

func (desc *descSource) Desc(labels prometheus.Labels) *prometheus.Desc {
	return prometheus.NewDesc(desc.name, desc.help, nil, labels)
}

var (
	namespace        = "container_state_"
	healthStatusDesc = descSource{
		namespace + "health_status",
		"Container health status."}
	statusDesc = descSource{
		namespace + "status",
		"Container status."}
	oomkilledDesc = descSource{
		namespace + "oomkilled",
		"Container was killed by OOMKiller."}
	startedatDesc = descSource{
		namespace + "startedat",
		"Time when the Container started."}
	finishedatDesc = descSource{
		namespace + "finishedat",
		"Time when the Container finished."}
	restartcountDesc = descSource{
		"container_restartcount",
		"Number of times the container has been restarted"}
	// Added 2026-08-10 with the non-fatal collector. 0 = a clean full sweep,
	// N > 0 = N containers did not answer their inspect within inspectTimeout and
	// are MISSING from this scrape — NOT "unreadable": the daemon answers those
	// calls 200, just later than we wait (see the collectErrors field comment),
	// -1 = the container list call itself failed (no container series at all).
	// Deliberately NOT in the drop regex of home-tech's docker-health scrape job.
	// ⚠ The HELP string below still says "their inspect failed" and is what the
	// SHIPPED image emits into Grafana; it is left alone here because changing it
	// only takes effect on a rebuild. Correct it with the next image rebuild.
	collectErrorsDesc = descSource{
		namespace + "collect_errors",
		"Containers skipped in the last collection because their inspect failed (-1 = list failed)."}
)

func (c *dockerHealthCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- healthStatusDesc.Desc(nil)
	ch <- statusDesc.Desc(nil)
	ch <- oomkilledDesc.Desc(nil)
	ch <- startedatDesc.Desc(nil)
	ch <- finishedatDesc.Desc(nil)
	ch <- restartcountDesc.Desc(nil)
	ch <- collectErrorsDesc.Desc(nil)
}

func (c *dockerHealthCollector) Collect(ch chan<- prometheus.Metric) {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := time.Now()
	if now.Sub(c.lastseen) >= cachePeriod {
		c.collectContainer()
		c.lastseen = now
	}
	c.collectMetrics(ch)
}

func (c *dockerHealthCollector) collectMetrics(ch chan<- prometheus.Metric) {
	for _, info := range c.containerInfoCache {
		var labels = map[string]string{}

		rep := regexp.MustCompile("[^a-zA-Z0-9_]")

		for k, v := range info.Config.Labels {
			label := strings.ToLower("container_label_" + k)
			labels[rep.ReplaceAllLiteralString(label, "_")] = v
		}
		labels["id"] = "/docker/" + info.ID
		labels["image"] = info.Config.Image
		labels["name"] = strings.TrimPrefix(info.Name, "/")

		b2f := func(b bool) float64 {
			if b {
				return 1
			}
			return 0
		}
		mapcopy := func(src map[string]string) prometheus.Labels {
			dst := map[string]string{}
			for k, v := range labels {
				dst[k] = v
			}
			return dst
		}

		for _, lv := range []string{"none", "starting", "healthy", "unhealthy"} {
			tmpLabels := mapcopy(labels)
			tmpLabels["status"] = lv
			ch <- prometheus.MustNewConstMetric(healthStatusDesc.Desc(tmpLabels), prometheus.GaugeValue, b2f(info.State.Health.Status == lv))
		}
		for _, lv := range []string{"paused", "restarting", "running", "removing", "dead", "created", "exited"} {
			tmpLabels := mapcopy(labels)
			tmpLabels["status"] = lv
			ch <- prometheus.MustNewConstMetric(statusDesc.Desc(tmpLabels), prometheus.GaugeValue, b2f(info.State.Status == lv))
		}
		ch <- prometheus.MustNewConstMetric(oomkilledDesc.Desc(labels), prometheus.GaugeValue, b2f(info.State.OOMKilled))
		// ⚠ WAS errCheck(err) ON BOTH PARSES — i.e. one container reporting a
		// timestamp this layout cannot parse killed the entire exporter, taking
		// every other container's metrics with it. Same fatal-on-one-bad-input
		// shape as the inspect bug below; degrade the single series instead.
		ch <- prometheus.MustNewConstMetric(startedatDesc.Desc(labels), prometheus.GaugeValue, tsSeconds(info.State.StartedAt))
		ch <- prometheus.MustNewConstMetric(finishedatDesc.Desc(labels), prometheus.GaugeValue, tsSeconds(info.State.FinishedAt))
		ch <- prometheus.MustNewConstMetric(restartcountDesc.Desc(labels), prometheus.GaugeValue, float64(info.RestartCount))
	}
	ch <- prometheus.MustNewConstMetric(collectErrorsDesc.Desc(nil), prometheus.GaugeValue, float64(c.collectErrors))
}

// tsSeconds parses a Docker RFC3339Nano timestamp into unix seconds, returning 0
// for anything unparseable (Docker uses the zero time "0001-01-01T00:00:00Z" for
// a container that never started/finished, which parses fine; this guards the
// genuinely malformed case without taking the process down for it).
func tsSeconds(v string) float64 {
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		errorLogger.Log("message", "unparseable container timestamp; reporting 0", "value", v, "err", err)
		return 0
	}
	return float64(t.Unix())
}

// collectContainer refreshes the inspect cache. It is DELIBERATELY non-fatal:
// every failure mode degrades the scrape rather than ending the process.
//
// ⚠ THE BUG THIS REPLACES (homebase incident 2026-08-10) — read before "simplifying":
// upstream called errCheck() on both the list and EVERY per-container inspect, and
// errCheck() is os.Exit(1). Combined with the unbounded context.Background() above,
// that meant a SINGLE container the daemon would not answer for (onyx-background,
// wedged such that its inspect never returned) hung the sweep for the socket-proxy's
// hardcoded 10m `timeout server`, took the resulting 504, and killed the exporter.
// restart: unless-stopped brought it straight back into the same wall, so the
// homebase's ONLY source of docker_container_* died every ~11 minutes for hours and
// every container alert rule went silently inert — the exact "green while doing
// nothing" failure the 2026-08-09 audit was written about. One sick container must
// never be able to blind the sensor for all the healthy ones.
func (c *dockerHealthCollector) collectContainer() {
	// One budget for the entire sweep; every per-call context descends from it, so
	// N unanswerable containers cost at most collectBudget in total rather than
	// N * inspectTimeout. Cancelled on return, which also releases any in-flight call.
	ctx, cancel := context.WithTimeout(context.Background(), collectBudget)
	defer cancel()

	lctx, lcancel := context.WithTimeout(ctx, listTimeout)
	containers, err := c.containerClient.ContainerList(lctx, types.ContainerListOptions{All: true})
	lcancel()
	if err != nil {
		// The list is all-or-nothing: without it we know about no containers at all.
		// CLEAR the cache rather than serving the previous sweep's data — stale
		// container state presented as current is precisely how a dead sensor reads
		// as a healthy homebase. Going empty here is SAFE and INTENDED: home-tech's
		// DockerContainerMetricsAbsent (absent(docker_container_running), for: 10m)
		// exists for exactly this case, and its hold-down absorbs a transient blip
		// while still catching a sustained one.
		errorLogger.Log("message", "container list failed; serving no container series this cycle", "err", err)
		c.containerInfoCache = []types.ContainerJSON{}
		c.collectErrors = -1
		return
	}

	fresh := make([]types.ContainerJSON, 0, len(containers))
	skipped := 0
	for _, container := range containers {
		ictx, icancel := context.WithTimeout(ctx, inspectTimeout)
		info, err := c.containerClient.ContainerInspect(ictx, container.ID)
		icancel()
		if err != nil {
			// SKIP THIS CONTAINER, KEEP THE REST. This single `continue` is the
			// fix: the 32 healthy containers still get their metrics while the one
			// wedged container is simply absent (and counted below).
			skipped++
			errorLogger.Log("message", "container inspect failed; skipping this container", "id", container.ID, "err", err)
			continue
		}

		// ⚠ THESE GUARDS NOW RUN BEFORE THE APPEND — upstream ran them after, which
		// made the Config guard INERT: types.ContainerJSON is appended BY VALUE, so
		// assigning info.Config afterwards only patched the caller's dead local copy
		// and left a nil Config in the cache for collectMetrics to dereference. (The
		// Health guard happened to work only because State is a POINTER and so is
		// shared with the appended copy — an accident, not a design.) Ordering them
		// ahead of the append makes both guards actually protect the cached value.
		if info.ContainerJSONBase == nil || info.State == nil {
			// Nothing this collector reports can be derived without these; the
			// metrics all read State.* and ID/Name off the base.
			skipped++
			errorLogger.Log("message", "container inspect returned no base/state; skipping", "id", container.ID)
			continue
		}
		if info.Config == nil {
			info.Config = &tcontainer.Config{Labels: map[string]string{}}
		}
		if info.State.Health == nil {
			info.State.Health = &types.Health{Status: "none"}
		}

		fresh = append(fresh, info)
	}

	// Swap in wholesale so a partial sweep never leaves the cache half-updated.
	c.containerInfoCache = fresh
	c.collectErrors = skipped
}

type loggerWrapper struct {
	Logger *log.Logger
}

func (l *loggerWrapper) Println(v ...interface{}) {
	(*l.Logger).Log("messages", v)
}

// Define loggers.
var (
	normalLogger = log.NewJSONLogger(log.NewSyncWriter(os.Stdout))
	errorLogger  = log.NewJSONLogger(log.NewSyncWriter(os.Stderr))
)

// errCheck terminates the process. As of 2026-08-10 it is reserved for STARTUP
// and LISTENER failures only — misconfiguration (an unusable DOCKER_HOST, a port
// we cannot bind) where dying fast and letting the restart policy retry is the
// right answer, and where there are no metrics to lose yet.
//
// ⚠ NEVER reintroduce it on the per-scrape collection path. Doing so is what let
// one wedged container crash-loop the exporter and take the homebase's entire
// container-monitoring contract dark — see collectContainer's header.
func errCheck(err error) {
	if err != nil {
		errorLogger.Log("message", err)
		os.Exit(1)
	}
}

// Define flags.
var (
	address = flag.String("listen-address", ":8080", "The address to listen on for HTTP requests.")
)

func init() {
	normalLogger = log.With(normalLogger, "timestamp", log.DefaultTimestampUTC)
	normalLogger = log.With(normalLogger, "severity", "info")
	errorLogger = log.With(errorLogger, "timestamp", log.DefaultTimestampUTC)
	errorLogger = log.With(errorLogger, "severity", "error")
	prometheus.MustRegister(prometheus.NewBuildInfoCollector())
}

func main() {
	flag.Parse()

	client, err := client.NewEnvClient()
	errCheck(err)
	defer client.Close()

	_, err = client.Ping(context.Background())
	errCheck(err)

	prometheus.MustRegister(&dockerHealthCollector{
		containerClient: client,
	})

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "<h1>docker state exporter</h1>")
	})

	http.HandleFunc("/-/healthy", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "up")
	})

	http.Handle("/metrics", promhttp.HandlerFor(
		prometheus.DefaultGatherer,
		promhttp.HandlerOpts{ErrorLog: &loggerWrapper{Logger: &errorLogger}, EnableOpenMetrics: true}))

	normalLogger.Log("message", "Server listening...", "address", address)

	server := &http.Server{Addr: *address, Handler: nil}

	go func() {
		err = server.ListenAndServe()
		if err != http.ErrServerClosed {
			errCheck(err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, os.Interrupt)
	<-quit
	normalLogger.Log("message", "Server shutting down...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		errorLogger.Log("message", fmt.Sprintf("Failed to gracefully shutdown: %v", err))
	}
	normalLogger.Log("message", "Server shutdown")
}
