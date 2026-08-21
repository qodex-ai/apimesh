package com.acme.web;

import com.acme.generated.api.HealthApi;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController implements HealthApi {
    public Object healthGet() { return null; }
    public Object healthDeepGet() { return null; }
}
