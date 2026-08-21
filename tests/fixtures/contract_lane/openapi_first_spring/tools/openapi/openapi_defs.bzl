# Server stub generation for all specs in this repo.
def openapi_spring_spec(name, spec_file, api_package, model_package):
    _gen = "java -jar openapi-generator-cli.jar generate -g spring " + \
        "--additional-properties interfaceOnly=true,useTags=true " + \
        "-i " + spec_file + " --api-package " + api_package
    native.genrule(name = name, cmd = _gen, srcs = [spec_file], outs = [name + ".srcjar"])
