package internal

import "net/http"

type Server struct{}

func (s Server) ListWidgets(w http.ResponseWriter, r *http.Request)  {}
func (s Server) CreateWidget(w http.ResponseWriter, r *http.Request) {}
