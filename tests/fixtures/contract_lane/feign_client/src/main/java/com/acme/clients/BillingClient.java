package com.acme.clients;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;

@FeignClient(name = "billing", url = "https://billing.internal.acme.com")
public interface BillingClient {
    @GetMapping("/invoices/{id}")
    Object getInvoice(@PathVariable("id") String id);
}
