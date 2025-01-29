import os
class Configurations:
    def __init__(self):
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
                    "qodexai-virtual-env", "swagger-bot", "repo_to_swagger"
                }
        self.routing_patters_map = {
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
        self.gpt_4o_model_name = "gpt-4o"
        self.user_config_file_dir = f"{os.getcwd()}/.qodexai"

