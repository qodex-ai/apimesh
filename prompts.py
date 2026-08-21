framework_identifier_prompt = """
        You are provided with a list of file names of a repository.
        You need to provide framework for the repo.
        File Names List:
        {file_paths}
        ----
        List of frameworks to choose from:
        {frameworks}
        ----
        Output Format:
        Your output should only be this json and the framework should be one of the framework provided in the list. If the detected framework is not provided in the list choose the similar framework from the provided list.
        {{"framework": ""}}
        """
framework_identifier_system_prompt = "You are a helpful assistant for understanding different framework repositories."









ruby_on_rails_swagger_generation_prompt = """
            You are an API documentation assistant specializing in Ruby on Rails applications. Generate Swagger (OpenAPI 3.0) JSON for the following Rails endpoint:

            Controller Information: {endpoint_info}
            Method: {endpoint_method}
            Path: {endpoint_path}
            Authentication/Authorization Information: {authentication_information}

            Include:
            1. description: A detailed description of the API endpoint's functionality and the parameter limitations.
            2. Expected request parameters (query, path, body) with fully resolved schemas. Do not use $ref or references; include all definitions inline.
            3. Example request and response schemas, fully expanded without references.
            4. Response codes (200, 400, etc.) and their descriptions.
            5. The method in the output should match the Method mentioned above.
            6. Tags should be UpperCamelCase, pluralized, and based on the Rails controller name inferred from the Controller Information.
            7. x-authorization-tag: This field should be 'Authorization Required' if the endpoint requires authorization(eg: bearer token, auth token etc.). Otherwise, set it to 'Authorization Not Required'.
            8. x-module-tag: This field will have the tag that represents the name of the module under which this endpoint exists.
            9. x-auth-tag: This field should be present only if the api handles user authentication and authorization processes like login, signup, signin, access token, email confirmation etc. The value should be 'Auth API'
            10. x-sensitive-information: Set to true if the endpoint exposes or processes sensitive information (PII, secrets, financial data, etc.) whose disclosure could harm people or the organization; otherwise set to false.


            Important Notes:
            - If the Controller Information or Authentication/Authorization Information indicates that the endpoint enforces authentication, include the appropriate security scheme in the JSON.
            - If the Controller Information specifies that the endpoint is public or exempt from authentication, do not include a security schema in the JSON.
            - Do not explicitly mention which endpoints require authentication in the instructions. Infer this dynamically based on the provided information.

            Additional Notes for Rails:
            - Infer parameter names from typical Rails conventions, such as `id` for resource paths or query parameters for filtering, sorting, etc.
            - Include request body schema for JSON payloads, typically used in `create` or `update` actions.
            - Response schemas should match typical Rails patterns, such as objects for `show` or arrays for `index`.

            Sample Output Format:
            ---> {{
            "openapi": "3.0.0",
            "info": {{
                "title": "User Management API",
                "version": "1.0.0"
            }},
            "paths": {{
                "/api/v1/users/{{id}}": {{
                    "get": {{
                        "summary": "Retrieve User Details",
                        "description": "Retrieves detailed information about a specific user using the provided ID.",
                        "tags": [
                            "Users"
                        ],
                        "parameters": [
                            {{
                                "name": "id",
                                "in": "path",
                                "required": true,
                                "schema": {{
                                    "type": "string"
                                }},
                                "description": "The unique identifier for the user."
                            }}
                        ],
                        "x-authorization-tag": "Authorization Not Required",
                        "x-module-tag": "Users",
                        "x-sensitive-information": false,
                        "responses": {{
                            "200": {{
                                "description": "User details retrieved successfully.",
                                "content": {{
                                    "application/json": {{
                                        "schema": {{
                                            "type": "object",
                                            "properties": {{
                                                "id": {{
                                                    "type": "string",
                                                    "example": "123"
                                                }},
                                                "name": {{
                                                    "type": "string",
                                                    "example": "John Doe"
                                                }},
                                                "email": {{
                                                    "type": "string",
                                                    "example": "john.doe@example.com"
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }},
                            "404": {{
                                "description": "User not found.",
                                "content": {{
                                    "application/json": {{
                                        "schema": {{
                                            "type": "object",
                                            "properties": {{
                                                "error": {{
                                                    "type": "string",
                                                    "example": "User not found"
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
            }}

            Output only valid JSON without any explanations.
        """
generic_swagger_generation_prompt = """
                        You are an API documentation assistant. Generate Swagger (OpenAPI 3.0) JSON for the following endpoint:

                        Method: {endpoint_method}
                        Path: {endpoint_path}
                        Additional Information: {endpoint_info}
                        Authentication/Authorization Information: {authentication_information}

                        Include:
                        1. description: A detailed description of the API endpoint's functionality and the parameter limitations.
                        2. Expected request parameters (query, path, body) with fully resolved schemas. Do not use $ref or references; include all definitions inline.
                        3. Example request and response schemas, fully expanded without references.
                        4. Response codes (200, 400, etc.) and their descriptions.
                        5. The method in the output should be same as the Method mentioned above.
                        6. Tags should be UpperCamelCase without space with pluralized form.
                        7. x-authorization-tag: This field should be 'Authorization Required' if the endpoint requires authorization(eg: bearer token, auth token etc.). Otherwise, set it to 'Authorization Not Required'.
                        8. x-module-tag: This field will have the tag that represents the name of the module under which this endpoint exists.
                        9. x-auth-tag: This field should be present only if the api handles user authentication and authorization processes like login, signup, signin, access token, email confirmation etc. The value should be 'Auth API'
                        10. x-sensitive-information: Set to true when the endpoint touches sensitive information that could harm people or the organization if exposed; otherwise false.


                        **Leverage Authentication/Authorization Information while generating parameters and headers for the endpoint.**

                        Ensure all components are fully expanded and self-contained. Do not include $ref in any part of the output.

                        Sample Output Format:
                        ---> {{
                        "openapi": "3.0.0",
                        "info": {{
                            "title": "User Confirmation API",
                            "version": "1.0.0"
                        }},
                        "paths": {{
                            "/api/v1/users/confirm_email": {{
                                "post": {{
                                    "summary": "Confirm User's Email",
                                    "description": "This endpoint confirms a user's email address based on the token sent to user's email.",
                                    "tags": [
                                        "Users"
                                    ],
                                    "requestBody": {{
                                        "required": true,
                                        "content": {{
                                            "application/json": {{
                                                "schema": {{
                                                    "type": "object",
                                                    "properties": {{
                                                        "token": {{
                                                            "type": "string",
                                                            "description": "The confirmation token sent to the user's email."
                                                        }}
                                                    }},
                                                    "required": [
                                                        "token"
                                                    ]
                                                }}
                                            }}
                                        }}
                                    }},
                                    "x-authorization-tag": "Authorization Not Required",
                                    "x-module-tag": "users",
                                    "x-auth-tag": "Auth API",
                                    "x-sensitive-information": false,
                                    "responses": {{
                                        "200": {{
                                            "description": "Email confirmed successfully",
                                            "content": {{
                                                "application/json": {{
                                                    "schema": {{
                                                        "type": "object",
                                                        "properties": {{
                                                            "message": {{
                                                                "type": "string",
                                                                "example": "Email confirmed successfully"
                                                            }}
                                                        }}
                                                    }}
                                                }}
                                            }}
                                        }},
                                        "422": {{
                                            "description": "Unprocessable Entity",
                                            "content": {{
                                                "application/json": {{
                                                    "schema": {{
                                                        "type": "object",
                                                        "oneOf": [
                                                            {{
                                                                "properties": {{
                                                                    "error": {{
                                                                        "type": "string",
                                                                        "example": "Token has expired"
                                                                    }}
                                                                }}
                                                            }},
                                                            {{
                                                                "properties": {{
                                                                    "message": {{
                                                                        "type": "string",
                                                                        "example": "Email already confirmed"
                                                                    }}
                                                                }}
                                                            }}
                                                        ]
                                                    }}
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                        }}

                        Output only valid JSON without any explanations.
                """

golang_swagger_generation_prompt = """
            You are an API documentation assistant specializing in Golang HTTP services
            (Gin, Echo, Fiber, Chi, Gorilla Mux, net/http). Generate Swagger (OpenAPI 3.0)
            JSON for the provided handler using only the supplied code and context.

            Method: {endpoint_method}
            Path: {endpoint_path}
            Handler Context:
            {endpoint_info}
            Authentication/Authorization Information:
            {authentication_information}

            Follow these rules exactly:
            1. description: Describe the endpoint’s purpose and parameter limitations.
            2. Request Parameters: Explicitly list query, path, and header parameters with
               fully resolved schemas (no $ref). Treat context hints such as `# header: x-user-id`
               as required headers unless explicitly optional.
            3. Request Body: When the handler binds or decodes JSON, describe the body schema
               and include an example.
            4. Responses: Include all inferred response codes (success + errors) with schemas
               and example payloads. Never omit error responses that appear in code.
            5. HTTP Method: The resulting spec must use the exact method provided above.
            6. Tags: Use UpperCamelCase, pluralized (e.g., "Users", "Orders").
            7. x-authorization-tag: "Authorization Required" when authentication is needed;
               otherwise "Authorization Not Required".
            8. x-module-tag: Use the controller/module name inferred from the handler.
            9. x-auth-tag: Include "Auth API" only for authentication-related routes
               (login, signup, token exchange, etc.).
            10. x-sensitive-information: true if the endpoint deals with sensitive data (PII, financial info, secrets) whose disclosure could harm people/the organization; otherwise false.

            Output must follow the sample OpenAPI structure shown below (same nesting, fields,
            and key ordering). Replace all placeholders with the real endpoint data.

            {{
              "openapi": "3.0.0",
              "info": {{
                "title": "Sample Title",
                "version": "1.0.0"
              }},
              "paths": {{
                "{endpoint_path}": {{
                  "{endpoint_method_lower}": {{
                    "summary": "Short one-line summary of the endpoint",
                    "description": "Longer description of what the endpoint does",
                    "tags": ["Example"],
                    "parameters": [
                      {{
                        "name": "id",
                        "in": "path",
                        "required": true,
                        "schema": {{"type": "string"}},
                        "description": "Resource identifier"
                      }}
                    ],
                    "requestBody": {{
                      "required": true,
                      "content": {{
                        "application/json": {{
                          "schema": {{
                            "type": "object",
                            "properties": {{
                              "name": {{"type": "string"}}
                            }}
                          }}
                        }}
                      }}
                    }},
                    "x-authorization-tag": "Authorization Required",
                    "x-module-tag": "Example",
                    "x-auth-tag": "",
                    "x-sensitive-information": false,
                    "responses": {{
                      "200": {{
                        "description": "Successful response",
                        "content": {{
                          "application/json": {{
                            "schema": {{"type": "object"}}
                          }}
                        }}
                      }},
                      "400": {{
                        "description": "Bad request"
                      }}
                    }}
                  }}
                }}
              }}
            }}

            The final answer must be valid JSON with no additional commentary or code fences.
        """

swagger_generation_system_prompt = "You are a helpful assistant for generating API documentation."

batch_swagger_generation_system_prompt = "You are an expert API documentation generator. You return only valid JSON, never prose or markdown fences."

batch_swagger_generation_prompt = """You are given the source context of one {framework_label} source file and a list of HTTP endpoints that file defines. Generate one OpenAPI 3.0 fragment covering EXACTLY the listed endpoints.

Rules:
1. The output is a single JSON object of the shape {{"paths": {{...}}}} and nothing else. No prose, no markdown fences.
2. "paths" must contain exactly one key per listed endpoint path, and under it exactly the listed lowercased HTTP method(s) for that path. Use the path and method EXACTLY as listed below. Do not rewrite, normalize, add, or omit endpoints.
3. Every operation must contain: "summary", "description" (detailed purpose and parameter limitations), "tags" (UpperCamelCase, pluralized), "parameters" (query and path parameters with fully resolved inline schemas, no $ref), "requestBody" when the method accepts one (fully resolved inline schemas), "responses" (all plausible codes with descriptions and example payloads), "x-authorization-tag" ("Authorization Required" or "Authorization Not Required"), "x-module-tag" (module or controller name), "x-auth-tag" ("Auth API" only for authentication endpoints, otherwise omit it), "x-sensitive-information" (true only when the endpoint handles data whose exposure could harm people or the organization).
4. Derive request and response schemas from the provided source context only. Do not invent fields the code does not show.
5. Shared context (models, helpers, authentication) applies to all endpoints; each endpoint also has its own handler source.
{framework_notes}

Endpoints to document (METHOD PATH, one per line):
{endpoints_list}

Shared file context:
{shared_context}

Per-endpoint handler sources:
{per_endpoint_sections}

Return only the JSON object described above."""

node_js_prompt = """
    You are given a **Node.js API definition** (such as an Express route handler `app.get("/path", (req, res) => {{...}})`, `router.post(...)`, or controller function) along with its context (request/response handling, variables used, and purpose).
    Using this, generate a valid **OpenAPI 3.0 specification** for that endpoint with the following rules:

    1. **description**: Write a detailed description of the API endpoint's purpose and parameter limitations.
    2. **Request Parameters**: Explicitly list query, path, and body parameters with **fully resolved schemas**. Do **not** use `$ref`.
    3. **Example Schemas**: Provide both example request and response schemas, fully expanded without references.
    4. **Responses**: Include all possible response codes (200, 400, 404, 422, etc.) with proper descriptions and example payloads.
    5. **HTTP Method**: The operation key in the spec must be exactly the lowercased HTTP method provided below, and the single key inside "paths" must be exactly the provided Route. Do not rewrite, normalize, or invent either.
    6. **Tags**: Tags should be **UpperCamelCase**, pluralized (e.g., `"Users"`, `"Orders"`).
    7. **x-authorization-tag**: Set to `"Authorization Required"` if the endpoint requires authentication (like tokens). Otherwise `"Authorization Not Required"`.
    8. **x-module-tag**: This should represent the module or controller name where the endpoint resides.
    9. **x-auth-tag**: Add `"Auth API"` only if the endpoint is handling authentication-related functionality (login, signup, password reset, confirmation, token exchange, etc.).
    10. **x-sensitive-information**: This boolean must be `true` when the endpoint processes sensitive information (PII, financial data, secrets, health data, etc.) that could harm people or the organization if exposed; otherwise `false`.


    The output must follow the structure of the provided sample OpenAPI spec:

    {{
      "openapi": "3.0.0",
      "info": {{
        "title": "User Confirmation API",
        "version": "1.0.0"
      }},
      "paths": {{
        "/api/v1/users/confirm_email": {{
          "post": {{
            "summary": "Confirm User's Email",
            "description": "This endpoint confirms a user's email address based on the token sent to user's email.",
            "tags": [
              "Users"
            ],
            "requestBody": {{
              "required": true,
              "content": {{
                "application/json": {{
                  "schema": {{
                    "type": "object",
                    "properties": {{
                      "token": {{
                        "type": "string",
                        "description": "The confirmation token sent to the user's email."
                      }}
                    }},
                    "required": [
                      "token"
                    ]
                  }}
                }}
              }}
            }},
            "x-authorization-tag": "Authorization Not Required",
            "x-module-tag": "users",
            "x-auth-tag": "Auth API",
            "x-sensitive-information": false,
            "responses": {{
              "200": {{
                "description": "Email confirmed successfully",
                "content": {{
                  "application/json": {{
                    "schema": {{
                      "type": "object",
                      "properties": {{
                        "message": {{
                          "type": "string",
                          "example": "Email confirmed successfully"
                        }}
                      }}
                    }}
                  }}
                }}
              }},
              "422": {{
                "description": "Unprocessable Entity",
                "content": {{
                  "application/json": {{
                    "schema": {{
                      "type": "object",
                      "oneOf": [
                        {{
                          "properties": {{
                            "error": {{
                              "type": "string",
                              "example": "Token has expired"
                            }}
                          }}
                        }},
                        {{
                          "properties": {{
                            "message": {{
                              "type": "string",
                              "example": "Email already confirmed"
                            }}
                          }}
                        }}
                      ]
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}


    Route:
    ---->{route}

    HTTP Method:
    ---->{http_method}

    Function Definition:
    ---->{function_definition}

    Context
    ---->{context}

    Based on the provided Node JS definition and context, generate a complete OpenAPI specification that adheres to these requirements. Ensure the output is valid JSON.
    No other explanation or reasoning is required"""

python_swagger_prompt = """
    Create an OpenAPI 3.0.0 specification in JSON format for a given Python API function definition. The input will include the Python function (e.g., `def get()`, `def post()`, etc.) and context about the functions and variables used. The generated OpenAPI spec must include:

    1. **description**: A detailed description of the API endpoint's functionality and parameter limitations.
    2. **Expected request parameters** (query, path, body) with fully resolved schemas, without using `$ref` or references; all definitions must be inline.
    3. **Example request and response schemas**, fully expanded without references.
    4. **Response codes** (e.g., 200, 400) with their descriptions.
    5. The operation key in the output must be exactly the lowercased HTTP method provided below (fall back to the method implied by the function only when no method is provided), and the single key inside "paths" must be exactly the provided Route. Do not rewrite, normalize, or invent either.
    6. **Tags** must be in UpperCamelCase, pluralized, and without spaces (e.g., `Users`, `Products`).
    7. **x-authorization-tag**: Set to 'Authorization Required' if the endpoint requires authorization (e.g., bearer token, auth token). Otherwise, set to 'Authorization Not Required'.
    8. **x-module-tag**: A tag representing the name of the module under which the endpoint exists (e.g., `users`, `products`).
    9. **x-auth-tag**: Include only if the API handles user authentication/authorization processes (e.g., login, signup, signin, access token, email confirmation). Set to 'Auth API'.
    10. **x-sensitive-information**: A boolean that must be true when the endpoint handles sensitive information (PII, credentials, PHI, secrets, etc.) whose exposure could harm people or the organization; otherwise false.

    The output must follow the structure of the provided sample OpenAPI spec:

    {{
      "openapi": "3.0.0",
      "info": {{
        "title": "User Confirmation API",
        "version": "1.0.0"
      }},
      "paths": {{
        "/api/v1/users/confirm_email": {{
          "post": {{
            "summary": "Confirm User's Email",
            "description": "This endpoint confirms a user's email address based on the token sent to user's email.",
            "tags": [
              "Users"
            ],
            "requestBody": {{
              "required": true,
              "content": {{
                "application/json": {{
                  "schema": {{
                    "type": "object",
                    "properties": {{
                      "token": {{
                        "type": "string",
                        "description": "The confirmation token sent to the user's email."
                      }}
                    }},
                    "required": [
                      "token"
                    ]
                  }}
                }}
              }}
            }},
            "x-authorization-tag": "Authorization Not Required",
            "x-module-tag": "users",
            "x-auth-tag": "Auth API",
            "x-sensitive-information": false,
            "responses": {{
              "200": {{
                "description": "Email confirmed successfully",
                "content": {{
                  "application/json": {{
                    "schema": {{
                      "type": "object",
                      "properties": {{
                        "message": {{
                          "type": "string",
                          "example": "Email confirmed successfully"
                        }}
                      }}
                    }}
                  }}
                }}
              }},
              "422": {{
                "description": "Unprocessable Entity",
                "content": {{
                  "application/json": {{
                    "schema": {{
                      "type": "object",
                      "oneOf": [
                        {{
                          "properties": {{
                            "error": {{
                              "type": "string",
                              "example": "Token has expired"
                            }}
                          }}
                        }},
                        {{
                          "properties": {{
                            "message": {{
                              "type": "string",
                              "example": "Email already confirmed"
                            }}
                          }}
                        }}
                      ]
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}


    Route:
    ---->{route}

    HTTP Method:
    ---->{http_method}

    Function Definition:
    ---->{function_definition}

    Context
    ---->{context}

    Based on the provided Python function definition and context, generate a complete OpenAPI specification that adheres to these requirements. Ensure the output is valid JSON.
    No other explanation or reasoning is required"""