package com.acme.impl;

import com.acme.generated.api.OrdersApiDelegate;
import org.springframework.stereotype.Service;

@Service
public class OrdersApiDelegateImpl implements OrdersApiDelegate {
    public Object listOrders() { return null; }
    public Object getOrder(String orderId) { return null; }
}
