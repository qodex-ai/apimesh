package com.acme.web;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class UsersController {
    @GetMapping("/users")
    public Object listUsers() { return null; }

    @PostMapping("/users")
    public Object createUser() { return null; }
}
