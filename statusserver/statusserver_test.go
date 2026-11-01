package statusserver

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"testing"
	"time"

	"controllerasg/model"
)

type dummyProvider struct {
	rec model.DecisionRecord
}

func (d *dummyProvider) LastRecord() model.DecisionRecord {
	return d.rec
}

// freePort returns an available TCP port for testing.
func freePort(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("no free port: %v", err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

func TestHealthAndStatusEndpoints(t *testing.T) {
	port := freePort(t)
	prov := &dummyProvider{
		rec: model.DecisionRecord{
			CycleID:  42,
			Decision: model.Maintain,
			Reason:   "test status ok",
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go Start(ctx, port, prov)

	// Wait for server to start
	urlHealth := fmt.Sprintf("http://127.0.0.1:%d/health", port)
	urlStatus := fmt.Sprintf("http://127.0.0.1:%d/status", port)

	var resp *http.Response
	var err error
	for i := 0; i < 20; i++ {
		time.Sleep(50 * time.Millisecond)
		resp, err = http.Get(urlHealth)
		if err == nil {
			break
		}
	}
	if err != nil {
		t.Fatalf("no se pudo conectar al servidor: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Errorf("health status = %d, want 200", resp.StatusCode)
	}

	// Test /status
	statusResp, err := http.Get(urlStatus)
	if err != nil {
		t.Fatalf("falló GET /status: %v", err)
	}
	defer statusResp.Body.Close()

	if statusResp.StatusCode != http.StatusOK {
		t.Errorf("status response code = %d, want 200", statusResp.StatusCode)
	}

	var rec model.DecisionRecord
	if err := json.NewDecoder(statusResp.Body).Decode(&rec); err != nil {
		t.Fatalf("error decodificando /status json: %v", err)
	}
	if rec.CycleID != 42 || rec.Reason != "test status ok" {
		t.Errorf("registro inesperado recibido de /status: %+v", rec)
	}
}

func TestStartWithZeroPortDoesNothing(t *testing.T) {
	// Should not block or panic
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	Start(ctx, 0, &dummyProvider{})
}
