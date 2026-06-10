### This PR introduces the following changes
- Detail
- Detail
- Add more details as needed

### Steps to Review
1. From a terminal in the project root run `git checkout develop`
2. Run `git fetch`
3. Run `git pull`
4. Check out the branch under test via `git checkout <branch name here>`
5. Install per the [repository README](https://github.com/daniel-pittman/bitbucket-cli#readme): the bash CLI needs only `curl` + `jq` (`bb` is on your `PATH`); the Python MCP server needs Python 3.10+ (`pip install -e ".[mcp,test]"`)
6. Run the bash syntax check `bash -n bb` and exercise the relevant `bb` subcommand
7. Run the Python test suite `python -m pytest tests/`
8. Add more steps as needed
