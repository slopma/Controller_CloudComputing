// Package statusserver expone un servidor HTTP mínimo con dos endpoints:
//
//   GET /health  → responde 200 OK siempre que el proceso esté vivo.
//                  Útil para health checks de systemd, load balancers o monitoreo.
//
//   GET /status  → devuelve el último DecisionRecord serializado como JSON.
//                  Permite inspeccionar el estado del controller sin leer el log.
//
// El servidor se arranca en una goroutine y se detiene limpiamente cuando el
// contexto recibido se cancela.
package statusserver

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"controllerasg/model"
)

// RecordProvider es la interfaz que debe implementar quien provea el último registro.
type RecordProvider interface {
	LastRecord() model.DecisionRecord
}

// Start arranca el servidor HTTP en el puerto indicado y lo detiene cuando ctx se cancela.
// Si port == 0, la función retorna inmediatamente sin arrancar nada.
func Start(ctx context.Context, port int, provider RecordProvider) {
	if port == 0 {
		return
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","time":%q}`, time.Now().Format(time.RFC3339))
	})

	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		rec := provider.LastRecord()
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(rec); err != nil {
			http.Error(w, "error serializando estado", http.StatusInternalServerError)
		}
	})

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", port),
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("[statusserver] escuchando en http://0.0.0.0:%d (GET /health, GET /status)", port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("[statusserver] error: %v", err)
		}
	}()

	<-ctx.Done()
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutCtx); err != nil {
		log.Printf("[statusserver] error al apagar: %v", err)
	}
}
