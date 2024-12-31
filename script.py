import os, json
from typing import List
from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
from langchain_openai import OpenAIEmbeddings
from langchain.vectorstores import FAISS
from openai import OpenAI
import re
import ast
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

class RepoToSwagger:
    def __init__(self, api_key: str, repo_path: str):
        """
        Initialize the converter with OpenAI API key and repository path
        """
        self.api_key = api_key
        self.repo_path = repo_path
        self.vector_store = None
        self.embeddings = OpenAIEmbeddings(model = "text-embedding-ada-002", openai_api_key=api_key)

        self.ignored_dirs = {
            '.git', 'node_modules', 'venv', '__pycache__', 'build', 'dist',
            'tests', 'test', 'docs', 'examples', 'migrations', 'tmp', 'vendor',
            'app/assets', "__pycache__", "build", "develop-eggs", "dist",
            "downloads", "eggs", ".eggs", "lib", "lib64", "parts", "sdist",
            "var", "wheels", ".egg-info", ".env", ".venv", "venv", "ENV",
            ".python-version", ".pytest_cache", ".coverage", "htmlcov",
            "log", "tmp", "db", "public", "coverage", "spec", "bundle",
            ".rvmrc", ".byebug_history", "storage", "node_modules",
            ".pnp", ".env.local", ".env.development.local", ".env.test.local",
            ".env.production.local", ".next", ".nuxt", ".DS_Store", ".tscache",
            ".angular", "dist-types", "target", ".apt_generated", ".classpath",
            ".factorypath", ".project", ".settings", ".springBeans", ".sts4-cache",
            ".gradle", "logs", ".idea", ".vscode",
            "qodexai-virtual-env"
        }

    def should_process_directory(self, dir_path: str) -> bool:
        """
        Check if a directory should be processed or ignored
        """
        path_parts = dir_path.split(os.sep)
        return not any(part in self.ignored_dirs for part in path_parts)

    def get_all_file_paths(self) -> List[str]:
        """
        Get all file paths in the repository, ignoring specified directories
        """
        file_paths = []
        supported_extensions = ('.py', '.js', '.ts', '.java', '.rb')

        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]

            if not self.should_process_directory(root):
                continue

            for file in files:
                if file.endswith(supported_extensions):
                    file_path = os.path.join(root, file)
                    file_paths.append(file_path)

        return file_paths

    def find_api_files(self, file_paths, patterns, framework):
        api_files = []
        for file_path in file_paths:
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
                    if any(re.search(pattern, content) for pattern in patterns):
                        if framework == "ruby_on_rails":
                            if file_path.endswith('.rb'):
                                api_files.append(file_path)
                        else:
                            api_files.append(file_path)
            except (UnicodeDecodeError, FileNotFoundError):
                continue
        return api_files

    def score_files(self, file_paths, patterns):
        file_scores = {}
        for file_path in file_paths:
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
                    matches = sum(len(re.findall(pattern, content)) for pattern in patterns)
                    if matches > 0:
                        file_scores[file_path] = matches
            except (UnicodeDecodeError, FileNotFoundError):
                continue
        return file_scores


    def extract_endpoints_with_gpt(self, file_path, framework):
        print("\n***************************************************")
        print(f"Started finding endpoints for {file_path}")
        with open(file_path, 'r', encoding='utf-8') as file:
            file_content = file.read()
        if framework == "ruby_on_rails":
            content = f"""
            You are a Ruby on Rails routing expert. Analyze the provided Rails routes file and return a comprehensive and VALID JSON array of all possible endpoints it defines. Ensure the following rules are strictly followed:

            1. **Consistency**:
               - Every output must follow the same format and include all valid endpoints.
               - Missing any valid endpoint or including invalid ones is unacceptable.

            2. **JSON Format**:
               - Output must be a JSON array of dictionaries.
               - Each dictionary should have exactly two keys: "method" and "path".
               - Example:
                 [
                   {{"method": "GET", "path": "/example"}},
                   {{"method": "POST", "path": "/example"}}
                 ]

            3. **Rules for Extraction**:
               a. **Direct Routes**:
                  - Include all HTTP methods (GET, POST, PUT, PATCH, DELETE).
                  - Include root, match, and custom routes.
                  - Include mounted engine paths.
                  - **Include routes being defined by devise gem in rails.**

               b. **Resources**:
                  - For `resources :name`:
                    * GET    /name              (index)   # (Exclude if `except: [:index]`) OR  (Include if `only: [:index]`) OR (Include if only and except is missing)
                    * POST   /name              (create)  # (Exclude if `except: [:create]`) OR  (Include if `only: [:create]`) OR (Include if only and except is missing)
                    * GET    /name/new          (new)     # (Exclude if `except: [:new]`) OR  (Include if `only: [:new]`) OR (Include if only and except is missing)
                    * GET    /name/:id/edit     (edit)    # (Exclude if `except: [:edit]`) OR  (Include if `only: [:edit]`) OR (Include if only and except is missing)
                    * GET    /name/:id          (show)    # (Exclude if `except: [:show]`) OR  (Include if `only: [:show]`) OR (Include if only and except is missing)
                    * PATCH  /name/:id          (update)  # (Exclude if `except: [:put]`) OR  (Include if `only: [:put]`) OR (Include if only and except is missing)
                    * PUT    /name/:id          (update)  # (Exclude if `except: [:put]`) OR  (Include if `only: [:put]`) OR (Include if only and except is missing)
                    * DELETE /name/:id          (destroy) # (Exclude if `except: [:destroy]`) OR  (Include if `only: [:destroy]`) OR (Include if only and except is missing)
                  - For `resource :name`:
                    * POST   /name              (create)  # (Exclude if `except: [:create]`) OR  (Include if `only: [:create]`) OR (Include if only and except is missing)
                    * GET    /name/new          (new)     # (Exclude if `except: [:new]`) OR  (Include if `only: [:new]`) OR (Include if only and except is missing)
                    * GET    /name/edit         (edit)    # (Exclude if `except: [:edit]`) OR  (Include if `only: [:edit]`) OR (Include if only and except is missing)
                    * GET    /name              (show)    # (Exclude if `except: [:show]`) OR  (Include if `only: [:show]`) OR (Include if only and except is missing)
                    * PATCH  /name              (update)  # (Exclude if `except: [:update]`) OR  (Include if `only: [:update]`) OR (Include if only and except is missing)
                    * PUT    /name              (update)  # (Exclude if `except: [:update]`) OR  (Include if `only: [:update]`) OR (Include if only and except is missing)
                    * DELETE /name              (destroy) # (Exclude if `except: [:destroy]`) OR  (Include if `only: [:destroy]`) OR (Include if only and except is missing)

               c. **Custom Routes**:
                  - Include collection and member routes.
                  - Include routes defined using `on: :member` or `on: :collection`.
                  - Include all namespace and scope prefixes.
                  - Include routes with constraints, custom paths, or shallow options.
                  - Include nested resources fully.

            4. **Validation**:
               - Ensure every path starts with `/`.
               - Ensure methods are uppercase (GET, POST, etc.).
               - Ensure paths are lowercase and include format suffixes like `.json` if specified.
               - Include both PATCH and PUT for updates.

            For Example if the content is:
            resources :test_plans, only: [:index, :create, :show, :update, :destroy] do
            member do
              post :run
            end

            Your output should be
            [{{"method":"DELETE", "path": "/api/v1/test_plans/:id"}}, {{"method":"PUT", "path": "/api/v1/test_plans/:id"}}, {{"method":"PATCH", "path": "/api/v1/test_plans/:id"}}, {{"method":"GET", "path": "/api/v1/test_plans/:id"}}, {{"method":"POST", "path": "/api/v1/test_plans"}}, {{"method":"GET", "path": "/api/v1/test_plans"}}, {{"method":"POST", "path": "/api/v1/test_plans/:id/run"}}]

            Return ONLY the JSON array, nothing else. Analyze this file:
            {file_content}
            """

            messages = [
                {"role": "system", "content": "You are an expert Ruby on Rails developer and routing specialist."},
                {"role": "user", "content": content}
            ]
        elif framework == "express":
            content = f"""
                    You are an Express.js routing expert. Analyze the provided JavaScript file defining Express routes and return a comprehensive and VALID JSON array of all possible endpoints it defines. Ensure the following rules are strictly followed:

                    1. **Consistency**:
                       - Every output must follow the same format and include all valid endpoints.
                       - Missing any valid endpoint or including invalid ones is unacceptable.

                    2. **JSON Format**:
                       - Output must be a JSON array of dictionaries.
                       - Each dictionary should have exactly two keys: "method" and "path".
                       - Example:
                         [
                           {{"method": "GET", "path": "/example"}},
                           {{"method": "POST", "path": "/example"}}
                         ]

                    3. **Rules for Extraction**:
                       a. **Direct Routes**:
                          - Include all HTTP methods (GET, POST, PUT, PATCH, DELETE).
                          - Include routes defined using `app.get`, `app.post`, etc.
                          - Include routes with middleware functions.

                       b. **Router Modules**:
                          - Extract routes defined using `express.Router()`.
                          - Ensure paths defined in the parent `app.use('/prefix', router)` are prepended to the router's routes.

                       c. **Dynamic and Static Paths**:
                          - Include dynamic parameters (e.g., `/users/:id`).
                          - Include optional parameters (e.g., `/users/:id?`).
                          - Include routes with query parameters explicitly if provided in comments or docs.

                       d. **Middlewares**:
                          - Include paths where routes are registered with middleware functions, e.g., `app.use('/api', apiRouter)`.

                       e. **Advanced Routes**:
                          - Include nested routers and route chaining.
                          - Include any routes registered via custom methods or libraries.

                    4. **Validation**:
                       - Ensure every path starts with `/`.
                       - Ensure methods are uppercase (GET, POST, etc.).
                       - Ensure paths are lowercase.
                       - Include all valid HTTP methods.

                    Analyze this file:

                    {file_content}
                    """

            # Messages for OpenAI
            messages = [
                {"role": "system", "content": "You are an expert Express.js developer and routing specialist."},
                {"role": "user", "content": content}
            ]
        # Call the OpenAI API
        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0)

        # Extract JSON content from the response
        content = response.choices[0].message.content
        start = content.find('[')
        end = content.rfind(']') + 1
        json_like_string = content[start:end]

        try:
            # Convert the JSON-like string to a Python list
            parsed_list = ast.literal_eval(json_like_string)
        except (ValueError, SyntaxError):
            print("Error parsing JSON-like string from GPT response")
            parsed_list = []

        print(f"Completed finding endpoints for {file_path}")
        return parsed_list

    def create_faiss_index(self, file_paths, framework):
        if framework == "ruby_on_rails":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.RUBY
            )
        elif framework == "express":
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                chunk_size=2000,
                chunk_overlap=200, language=Language.JS
            )
        else:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=2000,
                chunk_overlap=200
            )
        texts = []
        metadatas = []

        for file in file_paths:
            with open(file, 'r', encoding='utf-8') as file:
                file_content = file.read()
            chunks = text_splitter.split_text(file_content)
            texts.extend(chunks)
            metadatas.extend([{'file_path': str(file)}] * len(chunks))
        return FAISS.from_texts(texts, self.embeddings, metadatas=metadatas)

    def get_authentication_related_information(self, faiss_vector_db):
        query = "function to handle authentication information and authorization information"
        docs = faiss_vector_db.similarity_search(str(query), k=4)
        content_list = [doc.page_content.strip() for doc in docs]
        return content_list


    def get_endpoint_related_information(self, faiss_vector_db, endpoints):
        print("\n***************************************************")
        print(f"Started generating endpoint related information for {len(endpoints)} endpoints")
        start_time = time.time()
        completed = 0

        def process_endpoint(endpoint):
            query = f"This is the Method: {endpoint['method']} and this is the Endpoint Path: {endpoint['path']} fetch the controller information for the endpoint."
            docs = faiss_vector_db.similarity_search(str(query), k=4)
            content_list = [doc.page_content.strip() for doc in docs]
            return {'method': endpoint['method'], 'path': endpoint['path'], 'info': content_list}

        endpoint_related_content = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_endpoint = {executor.submit(process_endpoint, endpoint): endpoint
                                  for endpoint in endpoints}

            for future in as_completed(future_to_endpoint):
                endpoint_related_content.append(future.result())
                completed += 1
                end_time = time.time()
                print(
                    f"Completed generating endpoint related information for {completed} endpoints in {int(end_time - start_time)} seconds",
                    end="\r")

        return endpoint_related_content


    def generate_endpoint_swagger(self, endpoint, authentication_information, framework):

        client = OpenAI(
            api_key=self.api_key)
        if framework == "ruby_on_rails":
            prompt = f"""
            You are an API documentation assistant specializing in Ruby on Rails applications. Generate Swagger (OpenAPI 3.0) JSON for the following Rails endpoint:

            Controller Information: {endpoint['info']}
            Method: {endpoint['method']}
            Path: {endpoint['path']}
            Authentication/Authorization Information: {authentication_information}

            Include:
            1. A description for the endpoint derived from the Controller Information.
            2. Expected request parameters (query, path, body) with fully resolved schemas. Do not use $ref or references; include all definitions inline.
            3. Example request and response schemas, fully expanded without references.
            4. Response codes (200, 400, etc.) and their descriptions.
            5. The method in the output should match the Method mentioned above.
            6. Tags should be UpperCamelCase, pluralized, and based on the Rails controller name inferred from the Controller Information.

            Important Notes:
            - If the Controller Information or Authentication/Authorization Information indicates that the endpoint enforces authentication, include the appropriate security scheme in the JSON.
            - If the Controller Information specifies that the endpoint is public or exempt from authentication, do not include a security schema in the JSON.
            - Do not explicitly mention which endpoints require authentication in the instructions. Infer this dynamically based on the provided information.
            - For routes under `sessions` or `registrations` or `users` controllers, omit authentication requirements as these are part of user authentication flows.

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
                        "description": "Retrieves detailed information about a specific user based on their ID. This action is typically defined in the UsersController#show.",
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
        else:
            prompt = f"""
                        You are an API documentation assistant. Generate Swagger (OpenAPI 3.0) JSON for the following endpoint:

                        Method: {endpoint['method']}
                        Path: {endpoint['path']}
                        Additional Information: {endpoint['info']}
                        Authentication/Authorization Information: {authentication_information}

                        Include:
                        1. Description for the endpoint.
                        2. Expected request parameters (query, path, body) with fully resolved schemas. Do not use $ref or references; include all definitions inline.
                        3. Example request and response schemas, fully expanded without references.
                        4. Response codes (200, 400, etc.) and their descriptions.
                        5. The method in the output should be same as the Method mentioned above.
                        6. Tags should be UpperCamelCase without space with pluralized form.

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
                                    "description": "This endpoint is used to confirm a user's email address using a confirmation token.",
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
                                                                        "example": "Invalid token"
                                                                    }}
                                                                }}
                                                            }},
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
        messages = [
            {"role": "system", "content": "You are a helpful assistant for generating API documentation."},
            {"role": "user", "content": prompt}
        ]
        response = client.chat.completions.create(model="gpt-4o", messages=messages)
        response_content = response.choices[0].message.content
        try:
            start_index = response_content.find('{')
            end_index = response_content.rfind('}')
            swagger_json_block = response_content[start_index:end_index + 1]
            return json.loads(swagger_json_block)
        except Exception as ex:
            return {"paths": {endpoint['path']: {}}}


    def create_swagger_json(self, endpoints, authentication_information, framework, api_host):
        swagger = {
            "openapi": "3.0.0",
            "info": {
                "title": "Generated API Documentation",
                "version": "1.0.0",
                "description": "This Swagger file was generated using OpenAI GPT."
            },
            "servers": [
                {
                    "url": api_host
                }
            ],
            "paths": {}
        }
        print("\n***************************************************")
        print(f"\nstarted generating swagger for {len(endpoints)} endpoints")
        start_time = time.time()
        completed = 0

        def process_endpoint(endpoint):
            endpoint_swagger = self.generate_endpoint_swagger(endpoint, authentication_information, framework)
            return endpoint["path"], endpoint["method"].lower(), endpoint_swagger

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_endpoint = {executor.submit(process_endpoint, endpoint): endpoint
                                  for endpoint in endpoints}

            for future in as_completed(future_to_endpoint):
                path, method, endpoint_swagger = future.result()

                if path not in swagger["paths"]:
                    swagger["paths"][path] = {}

                key = list(endpoint_swagger['paths'].keys())[0]
                _method_list = list(endpoint_swagger['paths'][key].keys())
                if not _method_list:
                    continue
                _method = _method_list[0]
                swagger["paths"][path][_method] = endpoint_swagger['paths'][key][_method]

                completed += 1
                end_time = time.time()
                print(f"completed generating swagger for {completed} endpoints in {int(end_time - start_time)} seconds",
                      end="\r")

        return swagger

    def save_swagger_json(self, swagger, filename):
        """
        Saves the Swagger JSON to a file.

        Args:
            swagger (dict): The Swagger JSON dictionary.
            filename (str): The output file name.
        """
        with open(filename, 'w', encoding='utf-8') as file:
            json.dump(swagger, file, indent=2)
        print(f"Swagger JSON saved to {filename}.")

    def get_framework(self, file_paths, frameworks):
        client = OpenAI(
            api_key=self.api_key)
        prompt = f"""
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
        messages = [
            {"role": "system", "content": "You are a helpful assistant for understanding different framework repositories."},
            {"role": "user", "content": prompt}
        ]
        response = client.chat.completions.create(model="gpt-4o", messages=messages)
        response_content = response.choices[0].message.content
        start_index = response_content.find('{')
        end_index = response_content.rfind('}')
        swagger_json_block = response_content[start_index:end_index + 1]
        return json.loads(swagger_json_block)


    def generate_swagger(self, output_filepath, api_host):
        print("\n***************************************************")
        print("Started fetching all filepaths")
        file_paths = self.get_all_file_paths()
        print("Completed fetching all filepaths")

        routing_patters_map = {
            "ruby_on_rails": [
                r'\bresources\b.*:',
                r'namespace\b.*\'',
                r'Rails\.application\.routes\.draw',
                r'root\s+(?:\'|\")',
                r'get\s+[\'"]/\w+',
                r'post\s+[\'"]/\w+',
                r'put\s+[\'"]/\w+',
                r'delete\s+[\'"]/\w+'
            ],
            "django": [
                r'path\([\'"]',
                r'include\([\'"]',
                r'url\([\'"]',
                r'urlpatterns\s*=',
                r'@route\([\'"]',
                r'\.(?:get|post|put|delete)_api\(',
                r'@api_view\(\[[\'\"](?:GET|POST|PUT|DELETE)[\'\"]',
                r'ListAPIView',
                r'CreateAPIView',
                r'UpdateAPIView',
                r'DestroyAPIView'
            ],
            "express": [
                r'app\.(?:get|post|put|delete)\([\'"]',
                r'router\.(?:get|post|put|delete)\([\'"]',
                r'express\.Router\(\)',
                r'app\.use\([\'"]'
            ],
            "flask": [
                r'@app\.route\([\'"]',
                r'app\.(?:get|post|put|delete)\([\'"]',
                r'@blueprint\.route\([\'"]',
                r'flask\.Blueprint\(',
                r'app\.register_blueprint\(',
                r'@\w+\.route\([\'"]',
                r'Api\(',
                r'Resource\)',
                r'def (?:get|post|put|delete)\('
            ],
            "fastapi": [
                r'@app\.(?:get|post|put|delete)\([\'"]',
                r'@router\.(?:get|post|put|delete)\([\'"]',
                r'APIRouter\(\)',
                r'app\.include_router\(',
                r'@app\.middleware\([\'"]'
            ],
            "laravel": [
                r'Route::(?:get|post|put|delete)\([\'"]',
                r'Route::resource\([\'"]',
                r'Route::group\(',
                r'->middleware\([\'"]'
            ],
            "spring": [
                r'@RequestMapping\([\'"]',
                r'@GetMapping\([\'"]',
                r'@PostMapping\([\'"]',
                r'@PutMapping\([\'"]',
                r'@DeleteMapping\([\'"]',
                r'@RestController',
                r'@Controller',
                r'@RequestParam',
                r'@PathVariable'
            ]
        }

        print("\n***************************************************")
        print("Started framework identification")
        framework = self.get_framework(file_paths,list(routing_patters_map.keys()))['framework']
        print(f"completed framework identification - {framework}")

        print("\n***************************************************")
        print("Started finding files related to API information")
        api_files = self.find_api_files(file_paths, routing_patters_map[framework], framework)
        print("Completed finding files related to API information")

        all_endpoints = []
        for filePath in api_files:
            endpoints = self.extract_endpoints_with_gpt(filePath, framework)
            all_endpoints.extend(endpoints)

        print("\n***************************************************")
        print("Started creating faiss index for all files")
        faiss_vector = self.create_faiss_index(file_paths, framework)
        print("Completed creating faiss index for all files")
        print("Fetching authentication related information")
        authentication_information = self.get_authentication_related_information(faiss_vector)
        print("Completed Fetching authentication related information")
        endpoint_related_information = self.get_endpoint_related_information(faiss_vector, all_endpoints)

        swagger = self.create_swagger_json(endpoint_related_information, authentication_information, framework, api_host)
        self.save_swagger_json(swagger, output_filepath)
        return swagger



config_dir = f"{os.getcwd()}/.qodexai"
config_file = os.path.join(config_dir, "config.json")
os.makedirs(config_dir, exist_ok=True)
def load_config():
    if os.path.exists(config_file):
        with open(config_file, "r") as file:
            return json.load(file)
    return {}

def save_config(config):
    with open(config_file, "w") as file:
        json.dump(config, file, indent=4)


config = load_config()

print("***************************************************")
default_repo_path = config.get("repo_path", os.getcwd())
repo_path = input(f"Please enter the project repository path (default: {default_repo_path}): ") or default_repo_path
config["repo_path"] = repo_path
save_config(config)
# Check if the user entered something
if not repo_path.strip():
    print("No path provided. Exiting...")
    exit(1)
# Optionally check if the path exists
if os.path.isdir(repo_path):
    print("The directory exists.")
else:
    print("The directory does not exist.")
    exit(1)

print("***************************************************")
default_output_filepath = config.get("output_filepath", f"{os.getcwd()}/swagger.json")
output_filepath = input(f"Please enter the output file path (default: {default_output_filepath}): ") or default_output_filepath
config["output_filepath"] = output_filepath
save_config(config)

print("***************************************************")
default_openai_api_key = config.get("openai_api_key")
openai_api_key = input(f"Please enter openai api key (default: {default_openai_api_key}): ") or default_openai_api_key
config["openai_api_key"] = openai_api_key
save_config(config)

print("***************************************************")
default_api_host = config.get("api_host")
api_host = input(f"Please enter api host (default: {default_api_host}): ") or default_api_host
config["api_host"] = api_host
save_config(config)
# Check if the user entered something
if not api_host.strip():
    print("No api host provided. Exiting...")
    exit(1)

print("***************************************************")
default_qodex_api_key = config.get("qodex_api_key")
qodex_api_key = input(f"Please enter qodex api key (default: {default_qodex_api_key}) (press enter to skip this): ") or default_qodex_api_key
config["qodex_api_key"] = qodex_api_key
save_config(config)

converter = RepoToSwagger(api_key=openai_api_key, repo_path=repo_path)
code_files = converter.generate_swagger(output_filepath, api_host)
print("Process Completed Successfully")