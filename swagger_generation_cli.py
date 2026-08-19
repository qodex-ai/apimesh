import argparse
import os
import sys
import time
import traceback

from user_config import UserConfigurations
from swagger_generator import SwaggerGeneration
from file_scanner import FileScanner
from framework_identifier import FrameworkIdentifier
from endpoints_extractor import EndpointsExtractor
from faiss_index_generator import GenerateFaissIndex
from nodejs_pipeline.run_swagger_generation import run_swagger_generation as nodejs_swagger_generator
from python_pipeline.run_swagger_generation import run_swagger_generation as python_swagger_generator
from rails_pipeline.run_swagger_generation import run_swagger_generation as ruby_on_rails_swagger_generator
from golang_pipeline.run_swagger_generation import run_swagger_generation as golang_swagger_generator
from java_pipeline.run_swagger_generation import run_swagger_generation as java_swagger_generator
from utils import get_output_filepath
from telemetry_posthog import PostHogTelemetry


class NoEndpointsFound(Exception):
    """Raised when a run produced a spec with no paths, so nothing is worth writing."""
    pass


class RunSwagger:
    def __init__(self, project_api_key, openai_api_key, ai_chat_id, is_mcp, api_host=None, openai_model=None):
        self.ai_chat_id = ai_chat_id
        self.user_configurations = UserConfigurations(
            project_api_key, openai_api_key, ai_chat_id, is_mcp, api_host=api_host, openai_model=openai_model
        )
        self.user_config = self.user_configurations.load_user_config()
        self.framework_identifier = FrameworkIdentifier()
        self.file_scanner = FileScanner()
        self.endpoints_extractor = EndpointsExtractor()
        self.faiss_index = GenerateFaissIndex()
        self.swagger_generator = SwaggerGeneration()
        self.telemetry = PostHogTelemetry.from_env()


    def run_python_nodejs_ruby(self, framework):
        swagger = None
        try:
            if framework == "django" or framework == "flask" or framework == "fastapi":
                swagger = python_swagger_generator(self.user_config['api_host'])
            elif framework == "express" or framework == "nestjs":
                swagger = nodejs_swagger_generator(self.user_config['api_host'])
            elif framework == "ruby_on_rails":
                swagger = ruby_on_rails_swagger_generator(self.user_config['api_host'])
            elif framework == "golang":
                swagger = golang_swagger_generator(self.user_config['api_host'])
            elif framework == "spring":
                swagger = java_swagger_generator(self.user_config['api_host'])
        except Exception as ex:
            traceback.print_exc()
            print("Fallback to old procedure")
        return swagger


    def run(self, ai_chat_id=None):
        telemetry = self.telemetry
        run_id = telemetry.new_run_id()
        t0 = time.time()

        telemetry.capture("apimesh_run_started", {
            "run_id": run_id,
        })

        file_paths = []
        swagger = None
        all_endpoints = []
        framework = ""

        try:
            with telemetry.stage(run_id, "scan_repo"):
                file_paths = self.file_scanner.get_all_file_paths()
            if not file_paths:
                print("\n***************************************************")
                print("No supported source files were found in this repository")
                print("(looked for .py, .js, .ts, .java, .rb, .go).")
                print("Nothing was written and no API calls were made.")
                raise NoEndpointsFound("no supported source files")

            print("\n***************************************************")
            with telemetry.stage(run_id, "detect_framework"):
                try:
                    if self.user_config.get('framework', None):
                        print(f"Using Existing Framework - {self.user_config['framework']}")
                        framework = self.user_config.get('framework', "")
                    else:
                        print("Started framework identification")
                        framework = self.framework_identifier.get_framework(file_paths)['framework']
                        self.user_config['framework'] = framework
                        self.user_configurations.save_user_config(self.user_config)
                except Exception as ex:
                    msg = str(ex)
                    lowered = msg.lower()
                    if "insufficient_quota" in lowered or "quota" in lowered:
                        print("OpenAI quota exceeded. Please check your plan/billing and retry after adding credits.")
                    else:
                        print("We do not support this framework currently. Please contact QodexAI support.")
                    raise

            print(f"completed framework identification - {framework}")
            print("\n***************************************************")
            print("Started finding files related to API information")

            try:
                with telemetry.stage(run_id, "extract_endpoints"):
                    swagger = self.run_python_nodejs_ruby(framework)
                    if swagger:
                        print("Completed finding files related to API information")
                    else:
                        api_files = self.file_scanner.find_api_files(file_paths, framework)
                        print("Completed finding files related to API information")
                        for filePath in api_files:
                            endpoints = self.endpoints_extractor.extract_endpoints_with_gpt(filePath, framework)
                            all_endpoints.extend(endpoints)

                with telemetry.stage(run_id, "generate_swagger", {"fast_path": bool(swagger)}):
                    if not swagger and not all_endpoints:
                        # No endpoints were extracted; embedding the whole repo
                        # would spend real money to document nothing.
                        print("\n***************************************************")
                        print("No API endpoints were found in this repository.")
                        print("Nothing was written: swagger.json and the HTML viewer were not created.")
                        print(f"We detected the framework as '{framework or 'unknown'}'. If that is wrong,")
                        print("fix the \"framework\" value in apimesh/config.json and run again.")
                        raise NoEndpointsFound("no endpoints were extracted")
                    if not swagger:
                        print("\n***************************************************")
                        print("Started creating faiss index for all files")
                        faiss_vector = self.faiss_index.create_faiss_index(file_paths, framework)
                        print("Completed creating faiss index for all files")
                        print("Fetching authentication related information")
                        authentication_information = self.faiss_index.get_authentication_related_information(faiss_vector)
                        print("Completed Fetching authentication related information")
                        endpoint_related_information = self.endpoints_extractor.get_endpoint_related_information(faiss_vector, all_endpoints)
                        swagger = self.swagger_generator.create_swagger_json(endpoint_related_information, authentication_information, framework, self.user_config['api_host'])
            except NoEndpointsFound:
                raise
            except Exception:
                print("Oops! looks like we encountered an issue. Please try after some time.")
                raise

            if not swagger or not swagger.get("paths"):
                print("\n***************************************************")
                print("No API endpoints were found in this repository.")
                print("Nothing was written: swagger.json and the HTML viewer were not created.")
                print(f"We detected the framework as '{framework or 'unknown'}'. If that is wrong,")
                print("fix the \"framework\" value in apimesh/config.json and run again.")
                raise NoEndpointsFound("no endpoints were extracted")

            with telemetry.stage(run_id, "render_html"):
                output_filepath = get_output_filepath()
                html_ok = self.swagger_generator.save_swagger_json(swagger, output_filepath)

            telemetry.capture("apimesh_run_completed", {
                "run_id": run_id,
                "duration_ms": int((time.time() - t0) * 1000),
                "success": True,
            })
        except Exception as e:
            telemetry.capture("apimesh_run_failed", {
                "run_id": run_id,
                "duration_ms": int((time.time() - t0) * 1000),
                "success": False,
                "error_type": type(e).__name__,
            })
            raise
        return html_ok


def build_arg_parser():
    """
    The four positionals are kept exactly as they were so existing callers keep working.
    Everything new is an optional flag.
    """
    parser = argparse.ArgumentParser(
        prog="swagger_generation_cli",
        description="Scan the repository and generate an OpenAPI spec.",
    )
    parser.add_argument("openai_api_key", nargs="?", default="", help="OpenAI API key, or the OPENAI_API_KEY env var")
    parser.add_argument("project_api_key", nargs="?", default="", help="Qodex project API key")
    parser.add_argument("ai_chat_id", nargs="?", default="", help="Qodex AI chat id")
    parser.add_argument("is_mcp", nargs="?", default="", help="Non-empty when running under the MCP server")
    parser.add_argument(
        "--api-host",
        dest="api_host",
        default=None,
        help="Base URL written to servers[0].url, also settable via APIMESH_API_HOST",
    )
    parser.add_argument(
        "--model",
        dest="openai_model",
        default=None,
        help="OpenAI model to use, also settable via APIMESH_OPENAI_MODEL",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Write swagger.json only, skip the HTML viewer",
    )
    parser.add_argument(
        "--redetect-framework",
        action="store_true",
        help="Forget the cached framework and detect it again for this run",
    )
    return parser


def parse_args(argv=None):
    return build_arg_parser().parse_args(argv)


if __name__ == "__main__":
    args = parse_args()

    if args.no_html:
        os.environ["APIMESH_SKIP_HTML"] = "1"

    if args.redetect_framework and UserConfigurations.clear_cached_framework():
        print("Cleared the cached framework, it will be detected again for this run.")

    try:
        run_result = RunSwagger(
            args.project_api_key,
            args.openai_api_key,
            args.ai_chat_id,
            args.is_mcp,
            api_host=args.api_host,
            openai_model=args.openai_model,
        ).run(args.ai_chat_id)
    except NoEndpointsFound:
        # Message already printed, no traceback needed for an empty result.
        sys.exit(1)
    else:
        if run_result is False:
            # The spec was written but the requested HTML viewer failed.
            # Exit 2 tells a caller the run partially succeeded.
            sys.exit(2)
