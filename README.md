GIS-driven irrigation scheduler in use at https://alo.land

The program reads irrigation zone configuration from a PostGIS database,
generates a schedule based on a set number of daily slots, and uploads it to an
OpenSprinkler controller through the API.

This is unlikely to be useful out of the box for anyone else, but is published
in case it serves as useful inspiration.
