# Scope: what belongs here, what does not

This page exists so you do not have to guess where the edges of ciqkit are.

The criterion: **in scope is tooling any Connect IQ developer could use
against software already on their own machine, parameterized by config.**
Out of scope, for the *published* package, is automation of authenticated
Garmin online services — not because that kind of tooling is illegitimate,
but because publishing it carries costs that fall on everyone who runs it,
not just its author. The rest of this page explains the line and why it is
drawn where it is.

## What belongs here

Anything that runs **entirely on your own machine, against software you
already installed**:

* driving the Connect IQ Simulator (capture, input, navigation, test runs);
* scheduling access to it so concurrent users and agents do not corrupt each
  other;
* static analysis of screenshots and icons you produced;
* reading the Connect IQ SDK documentation that is on your disk, to answer API
  questions locally.

## What does not belong here

### Automating a Garmin online service

ciqkit contains no code that logs into, scrapes, or drives:

* the Connect IQ developer dashboard (`apps.garmin.com`) — app submission,
  store listings, screen images, beta tester lists;
* Garmin Connect — activities, FIT downloads, wellness data;
* the Connect IQ crash-report (ERA) backend;
* any other authenticated Garmin web property or private API.

There is no browser automation here, no dashboard selectors, no cookie handling
and no credential storage.

Three reasons it stays out — this is a publication rule, not a claim that such
tooling has no legitimate use; a private script is a different thing from a
published one:

1. **Terms of service.** Those services are governed by Garmin's terms, which
   you agree to as a developer or user. Automated access, and in particular
   working around bot protection, is the kind of thing those terms exist to
   restrict. A private script carries that exposure for the one person running
   it; a published tool hands the same exposure to everyone who installs it,
   most of whom will never have read this file.

2. **Web automation against someone else's dashboard cannot keep its
   promises.** It breaks whenever Garmin ships a redesign. A tool that works
   on the maintainer's machine this week and silently mis-clicks next month is
   worse than no tool, and "submit my app" is not a promise a published
   package can stand behind across releases it does not control.

3. **Blast radius.** A wrong click on a store dashboard can publish a broken
   build, overwrite a live listing, or expose a tester list — damage visible
   to your users, not just to you. Nothing in the published package should be
   able to do that.

If you are considering a contribution in this area, open an issue before
writing code — it is likely to be declined, and that is a judgement about
what a published package should carry, not about your work.


### Redistributing Garmin material

No SDK files, no API documentation text, no device images, no sample code from
Garmin. The SDK MCP server parses the docs on *your* machine and writes an
index that stays there; see [sdk-mcp.md](sdk-mcp.md).

If you find anything in this repository that looks like Garmin-owned content,
that is a bug — please report it.

### Anything that needs a physical watch

ciqkit talks to the simulator only. It does not sideload to a device, pair over
Bluetooth, or read from a connected watch. Some things genuinely can only be
verified on hardware (real BLE pairing, real sensor data, OS permission
dialogs); ciqkit will not pretend otherwise.

### A build system

ciqkit runs *your* build command; it does not replace `monkeyc`, manage jungle
files, or handle signing keys.

## Things that might arrive later

Not promises — just areas that would fit:

* richer navigation recipes (reach a named screen without hand-written coords);
* more static checks, if they can be made low-false-alarm.
