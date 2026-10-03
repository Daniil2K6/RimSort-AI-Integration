<p align="center">
    <h1 align="center">RimSort</h1>
    <p align="center">A free and open source multi-platform mod manager for the video game RimWorld.<br>
    Built from the ground up to be reliable and community managed.<br>
    Includes support for Linux, Mac, and Windows.</p>
    <p align="center"><strong><a href="https://github.com/Daniil2K6/RimSort-AI-Integration/releases">Releases</a> | <a href="https://rimsort.github.io/RimSort/">Wiki</a> | <a href="https://discord.gg/aV7g69JmR2">Discord</a></strong> </p>
    <br><br><br>
</p>

> **Fork notice:** This repository is a community fork of
> [RimSort/RimSort](https://github.com/RimSort/RimSort), maintained by Daniil2K6.
> It adds local AI integration (a built-in AI chat and an MCP server) on top of
> the upstream project. All credit for the original work goes to the RimSort team.

![RimSort Preview](./docs/assets/images/rimsort_preview.png)

## Installation

To install RimSort, visit the [Releases][Releases] page and download the latest zipped release for your operating system.

For Windows and Linux, unzip the download and run the `RimSort` executable inside the unzipped folder.

For macOS, make sure you grab the appropriate release per your CPU (x86_64 is for an Intel CPU Mac, ARM64 is for an Apple M1/M2 CPU Mac). You may need to follow [special instructions](https://rimsort.github.io/RimSort/user-guide/downloading-and-installing#macos) to get around Gatekeeper errors.

Check the [wiki][Wiki] for more detailed instructions.

## AI Assistant

**Built-in chat.** Open `View → AI Assistant` to get a dockable chat panel. It talks to
any OpenAI-compatible endpoint (OpenAI, OpenRouter, LM Studio, Ollama, ...) and drives
RimSort through the same tools as the MCP server: inspect installed and active mods,
build and validate the load order, sort it, save and apply modpacks, launch the game.
Destructive actions are confirmed in a dialog before they run. Configure the endpoint,
API key, model and system prompt in `Settings → AI Assistant`; the system prompt is
editable and can be reset to the built-in default.

**MCP server.** External AI clients can connect over stdio by running `python -m app mcp`
(see the [MCP guide][MCP]). The server exposes read-only inspection tools plus write
tools that ask for confirmation.

## Contributing

Bugs and feature requests are tracked in the [Issues][Issues] section of this repo. If you run into a bug or have a feature suggestion, feel free to create an Issue here yourself!

See the [wiki][Wiki] for detailed instructions on building RimSort yourself as well as guidelines for making pull requests.

Interested in helping translate RimSort to your language? Check out our [Translation Guidelines](https://rimsort.github.io/RimSort/development-guide/translation-guidelines) for detailed instructions on how to contribute translations.

## FAQ and Issues

If you have an issue, make sure you **checked the [wiki][Wiki]** for a solution.

[![Join us on Discord](https://github-production-user-asset-6210df.s3.amazonaws.com/2766946/248529301-486f4f8c-fed5-4fe1-832f-6461b7ce3a55.png)][Discord]

## Credits

- "Update" icon by [Icons8](https://icons8.com) ([icon link](https://icons8.com/icon/aPgBhcyogqyV/update)).

[Wiki]: https://rimsort.github.io/RimSort/
[Issues]: https://github.com/Daniil2K6/RimSort-AI-Integration/issues
[Releases]: https://github.com/Daniil2K6/RimSort-AI-Integration/releases
[Discord]: https://discord.gg/aV7g69JmR2
[MCP]: ./docs/user-guide/mcp.md
