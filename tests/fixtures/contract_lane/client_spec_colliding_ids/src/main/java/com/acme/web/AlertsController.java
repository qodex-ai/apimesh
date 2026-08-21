package com.acme.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class AlertsController {
    @GetMapping("/internal/alerts")
    public Object listAlerts() { return null; }

    public Object deleteAlert(String id) { return null; }
}
