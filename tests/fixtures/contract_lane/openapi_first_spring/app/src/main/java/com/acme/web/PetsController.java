package com.acme.web;

import com.acme.generated.api.PetsApi;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class PetsController implements PetsApi {
    public Object listPets() { return null; }
    public Object createPet() { return null; }
    public Object deletePet(String petId) { return null; }
}
