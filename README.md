# Pianoforte-deploy

Deployement script for [pianoforte](https://github.com/tilery/pianoforte).

## Dependencies

* Python >= 3.6.x


## Local installation

Create a python virtualenv:

    python -m venv venv

Then install the dependencies:

    pip install -r requirements.txt

By default, it expects the `pianoforte` source to be in `pianoforte/`, but you
can change that by either: passing the `--source-dir` option, setting the
`SOURCE_DIR` environment variable or setting the `source_dir` in the
config.


## Usage

Note: the scripts are tested on Ubuntu 16.04 and 18.04 servers only.

The tile stack is run under the Unix user `tilery`.

### Bootstraping the server

By default, the scripts try to deploy on a host named `pfmae`. You can
override the host by using the `--hostname` in any command.

    python remote bootstrap


### Send config files

Set the `SOURCE_DIR` env var pointing to the pianoforte folder (or pass it
to the command line as `--source-dir`). Then:

    python remote deploy

### Download OSM data and shapefiles

    python remote download

### Import OSM data

This will run the import command in a screen.

    python remote import

### Add geo indices

    python remote create-index


### Upload and import custom data

    python remote import-custom-data
