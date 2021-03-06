###############################################################################
## Tool name: Generate GTFS Route Shapes
## Step 1: Generate Shapes on Map
## Creator: Melinda Morang, Esri, mmorang@esri.com
## Last updated: 8 October 2015
###############################################################################
''' This tool generates a feature class of route shapes for GTFS data.
The route shapes show the geographic paths taken by the transit vehicles along
the streets or tracks. Each unique sequence of stop visits in the GTFS data will
get its own shape in the output feature class.  The user can edit the output
feature class shapes as desired.  Then, the user should use this feature class
and the other associated files in the output GDB as input to Step 2 in order
to create updated .txt files for use in the GTFS dataset.'''
################################################################################
'''Copyright 2015 Esri
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at
       http://www.apache.org/licenses/LICENSE-2.0
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.'''
################################################################################

import sqlite3, operator, os, re, csv, itertools
import arcpy

class CustomError(Exception):
    pass


# User input variables, set in the scripts that get input from the GUI
inGTFSdir = None
outDir = None
outGDBName = None
in_route_type_Street = None
in_route_type_Straight = None
inNetworkDataset = None
impedanceAttribute = None
driveSide = None
UTurn_input = None
restrictions = None
useJunctions = None
useNA = None

# Global derived variables
outGDB = None
SQLDbase = None
outSequencePoints = None
outRoutesfc = None
NoRouteGenerated = None

# Other global variables
# Use WGS coordinates because that's what the GTFS spec uses
WGSCoords = "GEOGCS['GCS_WGS_1984',DATUM['D_WGS_1984', \
SPHEROID['WGS_1984',6378137.0,298.257223563]], \
PRIMEM['Greenwich',0.0],UNIT['Degree',0.0174532925199433]]; \
-400 -400 1000000000;-100000 10000;-100000 10000; \
8.98315284119522E-09;0.001;0.001;IsHighPrecision"

# These are the GTFS files we need to use in this tool, so we will add them to a SQL database.
files_to_sqlize = ["stops", "stop_times", "trips", "routes"]


# ----- Main part of script -----
def RunStep1():
    '''Run Step 1 - Generate feature class of shapes for input to Step 2, which
    generates the actual GTFS shapes.txt file.'''

    try:
        # It's okay to overwrite stuff.
        orig_overwrite = arcpy.env.overwriteOutput
        arcpy.env.overwriteOutput = True

        # Check the user's version
        ArcVersionInfo = arcpy.GetInstallInfo("desktop")
        ArcVersion = ArcVersionInfo['Version']
        if ArcVersion == "10.0":
            arcpy.AddError("You must have ArcGIS 10.1 or higher to run this \
tool. You have ArcGIS version %s." % ArcVersion)
            raise CustomError
        if ArcVersion in ["10.1", "10.2"]:
            arcpy.AddWarning("Warning!  You can run Step 1 of this tool in \
ArcGIS 10.1 or 10.2, but you will not be able to run Step 2 without ArcGIS \
10.2.1 or higher.  You have ArcGIS version %s." % ArcVersion)

        #Check out the Network Analyst extension license
        if useNA:
            if arcpy.CheckExtension("Network") == "Available":
                arcpy.CheckOutExtension("Network")
            else:
                arcpy.AddError("The Network Analyst license is unavailable.")
                raise CustomError


    # ----- Set up the run, fix some inputs -----

        # Input format is a string separated by a ; ("0 - Tram, Streetcar, Light rail;3 - Bus;5 - Cable car")
        global route_type_Straight_textlist, route_type_Street_textlist, route_types_Straight, route_types_Street
        if in_route_type_Street:
            route_type_Street_textlist = in_route_type_Street.split(";")
        else:
            route_type_Street_textlist = []
        if in_route_type_Straight:
            route_type_Straight_textlist = in_route_type_Straight.split(";")
        else:
            route_type_Straight_textlist = []
        route_types_Street = []
        route_types_Straight = []
        for rtype in route_type_Street_textlist:
            route_types_Street.append(int(rtype.split(" - ")[0].strip('\'')))
        for rtype in route_type_Straight_textlist:
            route_types_Straight.append(int(rtype.split(" - ")[0].strip('\'')))

        # Set curb approach based on side of road vehicles drive on
        global CurbApproach
        driveSide = "Right"
        if driveSide == "Right":
            CurbApproach = 1 #"Right side of vehicle"
        else:
            CurbApproach = 2 #"Left side of vehcle"

        # Uturn policy is explained here: http://resources.arcgis.com/en/help/main/10.1/index.html#//00480000000n000000
        global UTurns
        if UTurn_input == "Allowed anywhere":
            UTurns = "ALLOW_UTURNS"
        elif UTurn_input == "Allowed only at intersections and dead ends":
            UTurns = "ALLOW_DEAD_ENDS_AND_INTERSECTIONS_ONLY"
        elif UTurn_input == "Allowed only at dead ends":
            UTurns = "ALLOW_DEAD_ENDS_ONLY"
        elif UTurn_input == "Not allowed anywhere":
            UTurns = "NO_UTURNS"

        # Sometimes, when locating stops, they snap to the closest street, which is
        # actually a side street instead of the main road where the stop is really
        # located. The Route results consequently have a lot of little loops or
        # spikes sticking out the side.  Sometimes we can improve results by
        # locating stops on network junctions instead of streets. Sometimes this
        # messes up the results, however, but we allow the users to try.
        global search_criteria
        if useJunctions:
            search_criteria = []
            NAdesc = arcpy.Describe(inNetworkDataset)
            for source in NAdesc.sources:
                if source.sourceType in ["JunctionFeature", "SystemJunction"]:
                    search_criteria.append([source.name, "SHAPE"])
                else:
                    search_criteria.append([source.name, "NONE"])
        else:
            search_criteria = "#"

        # Initialize a list for shapes that couldn't be generated from the route solver
        global NoRouteGenerated
        NoRouteGenerated = []

        # Set up the outputs
        global outGDB, outSequencePoints, outRoutesfc, outRoutesfcName, SQLDbase, outGDBName
        if not outGDBName.lower().endswith(".gdb"):
            outGDBName += ".gdb"
        outGDB = os.path.join(outDir, outGDBName)
        outSequencePointsName = "Stops_wShapeIDs"
        outSequencePoints = os.path.join(outGDB, outSequencePointsName)
        outRoutesfcName = "Shapes"
        outRoutesfc = os.path.join(outGDB, outRoutesfcName)
        SQLDbase = os.path.join(outGDB, "SQLDbase.sql")

        # Create output geodatabase
        arcpy.management.CreateFileGDB(outDir, outGDBName)


    # ----- Connect to the SQL database -----

        global c, conn
        conn = sqlite3.connect(SQLDbase)
        c = conn.cursor()


    # ----- SQLize the GTFS data -----

        try:
            SQLize_GTFS(files_to_sqlize)
        except Exception, err:
            arcpy.AddError("Error SQLizing the GTFS data.")
            raise


    # ----- Get lat/long for all stops and add to dictionary. Calculate location fields if necessary. -----

        arcpy.AddMessage("Collecting and processing GTFS stop information...")

        # Find all stops with lat/lon
        global stoplatlon_dict
        stoplatlon_dict = {}
        stoplatlonfetch = '''
            SELECT stop_id, stop_lat, stop_lon FROM stops
            ;'''
        c.execute(stoplatlonfetch)
        stoplatlons = c.fetchall()
        for stop in stoplatlons:
            # Add stop lat/lon to dictionary
            stoplatlon_dict[stop[0]] = [stop[1], stop[2]]

        # Calculate location fields for the stops and save them to a dictionary.
        if useNA:

            # Temporary feature class of stops for calculating location fields
            arcpy.management.CreateFeatureclass(outGDB, "TempStopswLocationFields", "POINT", "", "", "", WGSCoords)
            LocFieldStops = os.path.join(outGDB, "TempStopswLocationFields")
            arcpy.management.AddField(LocFieldStops, "stop_id", "TEXT")
            with arcpy.da.InsertCursor(LocFieldStops, ["SHAPE@X", "SHAPE@Y", "stop_id"]) as cur:
                for stop in stoplatlons:
                    # Insert stop into fc for location field calculation
                    cur.insertRow((float(stop[2]), float(stop[1]), stop[0]))

            # It would be easier to use CalculateLocations, but then we can't
            # exclude restricted network elements.
            # Instead, create a dummy Route layer and Add Locations
            RLayer = arcpy.na.MakeRouteLayer(inNetworkDataset, "DummyLayer", impedanceAttribute,
                        restriction_attribute_name=restrictions).getOutput(0)
            naSubLayerNames = arcpy.na.GetNAClassNames(RLayer)
            stopsSubLayer = naSubLayerNames["Stops"]
            fieldMappings = arcpy.na.NAClassFieldMappings(RLayer, stopsSubLayer)
            fieldMappings["Name"].mappedFieldName = "stop_id"
            arcpy.na.AddLocations(RLayer, stopsSubLayer, LocFieldStops, fieldMappings,
                        search_criteria=search_criteria,
                        snap_to_position_along_network="NO_SNAP",
                        exclude_restricted_elements="EXCLUDE")
            StopsLayer = arcpy.mapping.ListLayers(RLayer, stopsSubLayer)[0]

            # Iterate over the located stops and create a dictionary of location fields
            global stoplocfielddict
            stoplocfielddict = {}
            with arcpy.da.SearchCursor(StopsLayer, ["Name", "SourceID", "SourceOID", "PosAlong", "SideOfEdge"]) as cur:
                for stop in cur:
                    locfields = [stop[1], stop[2], stop[3], stop[4]]
                    stoplocfielddict[stop[0]] = locfields
            arcpy.management.Delete(StopsLayer)
            arcpy.management.Delete(LocFieldStops)


    # ----- Make dictionary of route info -----

        arcpy.AddMessage("Collecting GTFS route information...")

        # GTFS route_type information
        #0 - Tram, Streetcar, Light rail. Any light rail or street level system within a metropolitan area.
        #1 - Subway, Metro. Any underground rail system within a metropolitan area.
        #2 - Rail. Used for intercity or long-distance travel.
        #3 - Bus. Used for short- and long-distance bus routes.
        #4 - Ferry. Used for short- and long-distance boat service.
        #5 - Cable car. Used for street-level cable cars where the cable runs beneath the car.
        #6 - Gondola, Suspended cable car. Typically used for aerial cable cars where the car is suspended from the cable.
        #7 - Funicular. Any rail system designed for steep inclines.
        route_type_dict = {0: "Tram, Streetcar, Light rail",
                            1: "Subway, Metro",
                            2: "Rail",
                            3: "Bus",
                            4: "Ferry",
                            5: "Cable car",
                            6: "Gondola, Suspended cable car",
                            7: "Funicular"}

        # Find all routes and associated info.
        global RouteDict
        RouteDict = {}
        routesfetch = '''
            SELECT route_id, agency_id, route_short_name, route_long_name,
            route_desc, route_type, route_url, route_color, route_text_color
            FROM routes
            ;'''
        c.execute(routesfetch)
        routelist = c.fetchall()
        for route in routelist:
            # {route_id: [all route.txt fields + route_type_text]}
            try:
                route_type_text = route_type_dict[int(route[5])]
            except:
                route_type_text = "Other / Type not specified"
                route[5] = '100'
            RouteDict[route[0]] = [route[1], route[2], route[3], route[4], route[5],
                                     route[6], route[7], route[8],
                                     route_type_text]


    # ----- Match trip_ids with route_ids -----

        arcpy.AddMessage("Collecting GTFS trip information...")

        global trip_route_dict
        trip_route_dict = {}
        triproutefetch = '''
            SELECT trip_id, route_id FROM trips
            ;'''
        c.execute(triproutefetch)
        triproutelist = c.fetchall()
        for triproute in triproutelist:
            # {trip_id: route_id}
            trip_route_dict[triproute[0]] = triproute[1]

        # Find all trip_ids.
        triplist = []
        tripsfetch = '''
            SELECT DISTINCT trip_id FROM stop_times
            ;'''
        c.execute(tripsfetch)
        alltrips = c.fetchall()


    # ----- Create ordered stop sequences -----

        arcpy.AddMessage("Calculating unique sequences of stops...")

        # Select stops in that trip
        global sequence_shape_dict, shape_trip_dict
        sequence_shape_dict = {}
        shape_trip_dict = {}
        shape_id = 1
        for trip in alltrips:
            stopfetch = "SELECT stop_id, stop_sequence FROM stop_times WHERE trip_id='%s'" % trip[0]
            c.execute(stopfetch)
            selectedstops = c.fetchall()
            # Sort the stop list by sequence.
            selectedstops.sort(key=operator.itemgetter(1))
            stop_sequence = ()
            for stop in selectedstops:
                stop_sequence += (stop[0],)
            route_id = trip_route_dict[trip[0]]
            sequence_shape_dict_key = (route_id, stop_sequence)
            try:
                sh = sequence_shape_dict[sequence_shape_dict_key]
                shape_trip_dict.setdefault(sh, []).append(trip[0])
            except KeyError:
                sequence_shape_dict[sequence_shape_dict_key] = shape_id
                shape_trip_dict.setdefault(shape_id, []).append(trip[0])
                shape_id += 1

        numshapes = shape_id - 1
        arcpy.AddMessage("Your GTFS data contains %s unique shapes." % str(numshapes))


    # ----- Figure out which routes go with which shapes and update trips table -----

        global shape_route_dict
        shape_route_dict = {}
        for shape in shape_trip_dict:
            shaperoutes = []
            for trip in shape_trip_dict[shape]:
                shaperoutes.append(trip_route_dict[trip])
                # Update the trips table with the shape assigned to the trip
                updatetripstablestmt = "UPDATE trips SET shape_id='%s' WHERE trip_id='%s'" % (shape, trip)
                c.execute(updatetripstablestmt)
            conn.commit()
            shaperoutesset = set(shaperoutes)
            for route in shaperoutesset:
                shape_route_dict.setdefault(shape, []).append(route)
        conn.close()


    # ----- Generate street and straight routes -----

        # Create a points feature class for the stops to input for Routes
        # We'll save this so users can see the stop sequences with the shape_ids.
        arcpy.management.CreateFeatureclass(outGDB, outSequencePointsName, "POINT", "", "", "", WGSCoords)
        arcpy.management.AddField(outSequencePoints, "stop_id", "TEXT")
        arcpy.management.AddField(outSequencePoints, "shape_id", "LONG")
        arcpy.management.AddField(outSequencePoints, "sequence", "LONG")
        arcpy.management.AddField(outSequencePoints, "CurbApproach", "SHORT")
        if useNA:
            arcpy.management.AddField(outSequencePoints, "SourceID", "LONG")
            arcpy.management.AddField(outSequencePoints, "SourceOID", "LONG")
            arcpy.management.AddField(outSequencePoints, "PosAlong", "DOUBLE")
            arcpy.management.AddField(outSequencePoints, "SideOfEdge", "LONG")

        # Flag for whether we created the output fc in from Routes or if we need
        # to create it in the straight-line part
        Created_Street_Output = False

        # Generate shapes following the streets
        if route_types_Street:
            Generate_Shapes_Street()
            Created_Street_Output = True

        # Generate routes as straight lines between stops
        if route_types_Straight or NoRouteGenerated:
            Generate_Shapes_Straight(Created_Street_Output)


    # ----- Add route information to output feature class -----

        arcpy.AddMessage("Adding GTFS route information to output shapes feature class")

        # Explicitly set max allowed length for route_desc. Some agencies are wordy.
        max_route_desc_length = 250

        arcpy.management.AddField(outRoutesfc, "shape_id", "LONG")
        arcpy.management.AddField(outRoutesfc, "route_id", "TEXT")
        arcpy.management.AddField(outRoutesfc, "route_short_name", "TEXT")
        arcpy.management.AddField(outRoutesfc, "route_long_name", "TEXT")
        arcpy.management.AddField(outRoutesfc, "route_desc", "TEXT", "", "", max_route_desc_length)
        arcpy.management.AddField(outRoutesfc, "route_type", "SHORT")
        arcpy.management.AddField(outRoutesfc, "route_type_text", "TEXT")

        with arcpy.da.UpdateCursor(outRoutesfc, ["Name", "shape_id", "route_id",
                      "route_short_name", "route_long_name", "route_desc",
                      "route_type", "route_type_text"]) as ucursor:
            for row in ucursor:
                shape_id = row[0]
                route_id = shape_route_dict[int(shape_id)][0]
                route_short_name = RouteDict[route_id][1]
                route_long_name = RouteDict[route_id][2]
                route_desc = RouteDict[route_id][3]
                route_type = RouteDict[route_id][4]
                route_type_text = RouteDict[route_id][8]
                row[0] = row[0]
                row[1] = shape_id
                row[2] = route_id
                row[3] = route_short_name
                row[4] = route_long_name
                row[5] = route_desc[0:max_route_desc_length] if route_desc else route_desc #logic handles the case where it's empty
                row[6] = route_type
                row[7] = route_type_text
                ucursor.updateRow(row)


    # ----- Finish things up -----

        # Add output to map.
        if useNA:
            arcpy.SetParameterAsText(11, outRoutesfc)
            arcpy.SetParameterAsText(12, outSequencePoints)
        else:
            arcpy.SetParameterAsText(6, outRoutesfc)
            arcpy.SetParameterAsText(7, outSequencePoints)

        arcpy.AddMessage("Done!")
        arcpy.AddMessage("Output generated in " + outGDB + ":")
        arcpy.AddMessage("- Shapes")
        arcpy.AddMessage("- Stops_wShapeIDs")

    except CustomError:
        arcpy.AddError("Error generating shapes feature class from GTFS data.")
        pass

    except Exception, err:
        raise

    finally:
        arcpy.env.overwriteOutput = orig_overwrite


def SQLize_GTFS(files_to_sqlize):
    ''' SQLize the GTFS data'''
    arcpy.AddMessage("SQLizing the GTFS data...")
    arcpy.AddMessage("(This step might take a while for large datasets.)")

    # Schema of standard GTFS, with a 1 or 0 to indicate if the field is required
    sql_schema = {
            "stops" : {
                    "stop_id" :     ("TEXT", 1),
                    "stop_code" :   ("TEXT", 0),
                    "stop_name" :   ("TEXT", 1),
                    "stop_desc" :   ("TEXT", 0),
                    "stop_lat" :    ("REAL", 1),
                    "stop_lon" :    ("REAL", 1),
                    "zone_id" :     ("TEXT", 0),
                    "stop_url" :    ("TEXT", 0),
                    "location_type" : ("INTEGER", 0),
                    "parent_station" : ("TEXT", 0),
                    "stop_timezone" :   ("TEXT", 0),
                    "wheelchair_boarding": ("INTEGER", 0)
                } ,
            "stop_times" : {
                    "trip_id" :     ("TEXT", 1),
                    "arrival_time" :    ("TEXT", 1),
                    "departure_time" :  ("TEXT", 1),
                    "stop_id" :         ("TEXT", 1),
                    "stop_sequence" :   ("INTEGER", 1),
                    "stop_headsign" :   ("TEXT", 0),
                    "pickup_type" :     ("INTEGER", 0),
                    "drop_off_type" :   ("INTEGER", 0),
                    "shape_dist_traveled" : ("REAL", 0)
                } ,
            "trips" : {
                    "route_id" :    ("TEXT", 1),
                    "service_id" :  ("TEXT", 1),
                    "trip_id" :     ("TEXT", 1),
                    "trip_headsign" :   ("TEXT", 0),
                    "trip_short_name" :     ("TEXT", 0),
                    "direction_id" : ("INTEGER", 0),
                    "block_id" :    ("TEXT", 0),
                    "shape_id" :    ("TEXT", 0),
                    "wheelchair_accessible" : ("INTEGER", 0)
                } ,
            "routes" : {
                    "route_id" :    ("TEXT", 1),
                    "agency_id" :  ("TEXT", 0),
                    "route_short_name": ("TEXT", 0),
                    "route_long_name":  ("TEXT", 0),
                    "route_desc":   ("TEXT", 0),
                    "route_type":   ("INTEGER", 1),
                    "route_url":    ("TEXT", 0),
                    "route_color":  ("TEXT", 0),
                    "route_text_color": ("TEXT", 0),
                } ,
        }


    # SQLize each file we care about, using its own schema and ordering
    for GTFSfile in files_to_sqlize:
        # Note: a check for existance of each required file is in tool validation

        # Open the file for reading
        fname = os.path.join(inGTFSdir, GTFSfile) + ".txt"
        f = open(fname, "rb")
        reader = csv.reader(f)

        # Put everything in utf-8 to handle BOMs and weird characters.
        # Eliminate blank rows (extra newlines) while we're at it.
        reader = ([x.decode('utf-8-sig').strip() for x in r] for r in reader if len(r) > 0)

        # First row is column names:
        columns = [name.strip() for name in reader.next ()]

        # Set up the table schema
        schema = ""
        for col in columns:
            try:
                # Read the data type from the GTFS schema dictionary
                schema = schema + col + " " + sql_schema[GTFSfile][col][0] + ", "
            except KeyError:
                # If they're using a custom field, preserve it and assume it's text.
                schema = schema + col + " TEXT, "
        schema = schema[:-2]

        # Make sure file has all the required fields
        for col in sql_schema[GTFSfile]:
            if sql_schema[GTFSfile][col][1] == 1:
                if not col in columns:
                    arcpy.AddError("GTFS file " + GTFSfile + ".txt is missing required field '" + col + "'.")
                    raise CustomError

        # Make sure lat/lon values are valid
        if GTFSfile == "stops":
            rows = check_latlon_fields(reader, columns, fname)
        # Otherwise just leave them as they are
        else:
            rows = reader

        # Create the SQL table
        c.execute("DROP TABLE IF EXISTS %s;" % GTFSfile)
        create_stmt = "CREATE TABLE %s (%s);" % (GTFSfile, schema)
        c.execute(create_stmt)
        conn.commit()

        # Add the data to the table
        values_placeholders = ["?"] * len(columns)
        c.executemany("INSERT INTO %s (%s) VALUES (%s);" %
                            (GTFSfile,
                            ",".join(columns),
                            ",".join(values_placeholders))
                        , rows)
        conn.commit()

        # If optional columns in routes weren't included in the original data, add them so we don't encounter errors later.
        if GTFSfile == "routes":
            for col in sql_schema["routes"]:
                if not col in columns:
                    c.execute("ALTER TABLE routes ADD COLUMN %s %s" % (col, sql_schema[GTFSfile][col][0]))
                    conn.commit()

        # If our original data did not have a shape-related fields, add them.
        if GTFSfile == "trips":
            if 'shape_id' not in columns:
                c.execute("ALTER TABLE trips ADD COLUMN shape_id TEXT")
                conn.commit()
        if GTFSfile == "stop_times":
            if 'shape_dist_traveled' not in columns:
                c.execute("ALTER TABLE stop_times ADD COLUMN shape_dist_traveled REAL")
                conn.commit()

        f.close ()

    #  Generate indices
    c.execute("CREATE INDEX stoptimes_index_tripIDs ON stop_times (trip_id);")
    c.execute("CREATE INDEX trips_index_tripIDs ON trips (trip_id);")


def check_latlon_fields(rows, col_names, fname):
    '''Ensure lat/lon fields are valid'''
    def check_latlon_cols(row):
        stop_id = row[col_names.index("stop_id")]
        stop_lat = row[col_names.index("stop_lat")]
        stop_lon = row[col_names.index("stop_lon")]
        try:
            stop_lat_float = float(stop_lat)
        except ValueError:
            msg = 'stop_id "%s" in %s contains an invalid non-numerical value \
for the stop_lat field: "%s". Please double-check all lat/lon values in your \
stops.txt file.' % (stop_id, fname, stop_lat)
            arcpy.AddError(msg)
            raise CustomError
        try:
            stop_lon_float = float(stop_lon)
        except ValueError:
            msg = 'stop_id "%s" in %s contains an invalid non-numerical value \
for the stop_lon field: "%s". Please double-check all lat/lon values in your \
stops.txt file.' % (stop_id, fname, stop_lon)
            arcpy.AddError(msg)
            raise CustomError
        if not (-90.0 <= stop_lat_float <= 90.0):
            msg = 'stop_id "%s" in %s contains an invalid value outside the \
range (-90, 90) the stop_lat field: "%s". stop_lat values must be in valid WGS 84 \
coordinates.  Please double-check all lat/lon values in your stops.txt file.\
' % (stop_id, fname, stop_lat)
            arcpy.AddError(msg)
            raise CustomError
        if not (-180.0 <= stop_lon_float <= 180.0):
            msg = 'stop_id "%s" in %s contains an invalid value outside the \
range (-180, 180) the stop_lon field: "%s". stop_lon values must be in valid WGS 84 \
coordinates.  Please double-check all lat/lon values in your stops.txt file.\
' % (stop_id, fname, stop_lon)
            arcpy.AddError(msg)
            raise CustomError
        return row
    return itertools.imap(check_latlon_cols, rows)


def Generate_Shapes_Street():
    '''Generate preliminary shapes for each route by calculating the optimal
    route along the network with the Network Analyst Route solver.'''

    arcpy.AddMessage("Generating on-street route shapes for routes of the following types, if they exist in your data:")
    for rtype in route_type_Street_textlist:
        arcpy.AddMessage(rtype)
    arcpy.AddMessage("(This step may take a while for large GTFS datasets.)")


    # ----- Writing stops in sequence to feature class for Route input -----

    arcpy.AddMessage("- Preparing stops")

    # Extract only the sequences we want to make street-based shapes for.
    sequences_Streets = []
    for sequence in sequence_shape_dict:
        shape_id = sequence_shape_dict[sequence]
        route_id = sequence[0]
        route_type = RouteDict[route_id][4]
        if route_type in route_types_Street:
            sequences_Streets.append(sequence)

    # Chunk the sequences so we don't run out of memory in the Route solver.
    ChunkSize = 100
    sequences_Streets_chunked = []
    for i in xrange(0, len(sequences_Streets), ChunkSize):
        sequences_Streets_chunked.append(sequences_Streets[i:i+ChunkSize])

    # Huge loop over each chunk.
    totchunks = len(sequences_Streets_chunked)
    chunkidx = 1
    global NoRouteGenerated
    badStops = []
    unlocated_stops = []
    for chunk in sequences_Streets_chunked:

        arcpy.AddMessage("- Calculating Routes part %s of %s." % (str(chunkidx), str(totchunks)))
        chunkidx += 1

        InputRoutePoints = arcpy.management.CreateFeatureclass(outGDB, "TempInputRoutePoints", "POINT", outSequencePoints, "", "", WGSCoords)

        # Add the StopPairs table to the feature class.
        shapes_in_chunk = []
        with arcpy.da.InsertCursor(InputRoutePoints, ["SHAPE@X", "SHAPE@Y", "shape_id", "sequence", "CurbApproach", "stop_id", "SourceID", "SourceOID", "PosAlong", "SideOfEdge"]) as cur:
            for sequence in chunk:
                shape_id = sequence_shape_dict[sequence]
                shapes_in_chunk.append(shape_id)
                sequence_num = 1
                for stop in sequence[1]:
                    try:
                        stop_lat = stoplatlon_dict[stop][0]
                        stop_lon = stoplatlon_dict[stop][1]
                        SourceID = stoplocfielddict[stop][0]
                        SourceOID = stoplocfielddict[stop][1]
                        PosAlong = stoplocfielddict[stop][2]
                        SideOfEdge = stoplocfielddict[stop][3]
                    except KeyError:
                        badStops.append(stop)
                        sequence_num += 1
                        continue
                    cur.insertRow((float(stop_lon), float(stop_lat), shape_id, sequence_num, CurbApproach, stop, SourceID, SourceOID, PosAlong, SideOfEdge))
                    sequence_num += 1


        # ----- Generate routes ------

        # Note: The reason we use hierarchy is to ensure that the entire network doesn't gets searched
        # if a route can't be found between two points
        RLayer = arcpy.na.MakeRouteLayer(inNetworkDataset, "TransitShapes", impedanceAttribute,
                    find_best_order="USE_INPUT_ORDER",
                    UTurn_policy=UTurns,
                    restriction_attribute_name=restrictions,
                    hierarchy="USE_HIERARCHY",
                    output_path_shape="TRUE_LINES_WITH_MEASURES").getOutput(0)

        # To refer to the Route sublayers, get the sublayer names.  This is essential for localization.
        naSubLayerNames = arcpy.na.GetNAClassNames(RLayer)
        stopsSubLayer = naSubLayerNames["Stops"]

        # Map fields to ensure that each shape gets its own route.
        fieldMappings = arcpy.na.NAClassFieldMappings(RLayer, stopsSubLayer, True)
        fieldMappings["RouteName"].mappedFieldName = "shape_id"
        fieldMappings["CurbApproach"].mappedFieldName = "CurbApproach"
        fieldMappings["SourceID"].mappedFieldName = "SourceID"
        fieldMappings["SourceOID"].mappedFieldName = "SourceOID"
        fieldMappings["PosAlong"].mappedFieldName = "PosAlong"
        fieldMappings["SideOfEdge"].mappedFieldName = "SideOfEdge"

        arcpy.na.AddLocations(RLayer, stopsSubLayer, InputRoutePoints, fieldMappings,
                    sort_field="sequence",
                    append="CLEAR")

        # Use a simplification tolerance on Solve to reduce the number of vertices
        # in the output lines (to make shapes.txt files smaller and to make the
        # linear referencing quicker.
        simpTol = "2 Meters"
        try:
            SolvedLayer = arcpy.na.Solve(RLayer, ignore_invalids=True, simplification_tolerance=simpTol)
        except:
            arcpy.AddWarning("Unable to create on-street Routes because the Solve failed.")
            arcpy.AddWarning("Solve warning messages:")
            arcpy.AddWarning(arcpy.GetMessages(1))
            arcpy.AddWarning("Solve error messages:")
            arcpy.AddWarning(arcpy.GetMessages(2))
            NoRouteGenerated += shapes_in_chunk
            continue

        # If any of the routes couldn't be solved, they will leave a warning.
        # Save the shape_ids so we can generate straight-line routes for them.
        # Similarly, if any stops were skipped because they were unlocated, they will leave a warning.
        warnings = arcpy.GetMessages(1)
        warninglist = warnings.split("\n")
        for w in warninglist:
            if re.match('No route for ', w):
                thingsInQuotes = re.findall('"(.+?)"', w)
                NoRouteGenerated.append(int(thingsInQuotes[0]))
            elif re.search(' is unlocated.', w):
                thingsInQuotes = re.findall('"(.+?)"', w)
                unlocated_stops.append(thingsInQuotes[0])

        # Make layer objects for each sublayer we care about.
        RoutesLayer = arcpy.mapping.ListLayers(RLayer, naSubLayerNames["Routes"])[0]


    # ----- Save routes to feature class -----

        # Uncomment this if you want to save the Stops layer from Route.
        ##StopsLayer = arcpy.mapping.ListLayers(RLayer, stopsSubLayer)[0]
        ##arcpy.CopyFeatures_management(StopsLayer, os.path.join(outGDB, "TestOutStops"))

        # Save the output routes.
        if not arcpy.Exists(outRoutesfc):
            arcpy.management.CopyFeatures(RoutesLayer, outRoutesfc)
        else:
            arcpy.management.Append(RoutesLayer, outRoutesfc)

        arcpy.management.Delete(SolvedLayer)

        # Add the stop sequences to the final output FC and delete the temporary one.
        arcpy.management.Append(InputRoutePoints, outSequencePoints)
        arcpy.management.Delete(InputRoutePoints)

    if NoRouteGenerated:
        arcpy.AddWarning("On-street route shapes for the following shape_ids could \
not be generated.  Straight-line route shapes will be generated for these \
shape_ids instead:")
        arcpy.AddWarning(sorted(NoRouteGenerated))
        arcpy.AddWarning("If you are unhappy with this result, try re-running your \
analysis with a different u-turn policy and/or network restrictions, and check your \
network dataset for connectivity problems.")

    if badStops:
        badStops = sorted(list(set(badStops)))
        arcpy.AddWarning("Your stop_times.txt lists times for the following stops which are not included in your stops.txt file. These stops will be ignored. " + unicode(badStops))

    if unlocated_stops:
        unlocated_stops = sorted(list(set(unlocated_stops)))
        arcpy.AddWarning("The following stop_ids could not be located on your network dataset and were skipped when route shapes were generated.  \
If you are unhappy with this result, please double-check your stop_lat and stop_lon values in stops.txt and your network dataset geometry \
to make sure everything is correct.")


def Generate_Shapes_Straight(Created_Street_Output):
    '''Generate route shapes as straight lines between stops.'''

    arcpy.AddMessage("Generating straight-line route shapes for routes of the following types, if they exist in your data:")
    for rtype in route_type_Straight_textlist:
        arcpy.AddMessage(rtype)
    arcpy.AddMessage("(This step may take a while for large GTFS datasets.)")

    # If we didn't already create the output feature class with the Street-based routes, create it now.
    if not Created_Street_Output or not arcpy.Exists(outRoutesfc):
        arcpy.management.CreateFeatureclass(outGDB, outRoutesfcName, "POLYLINE", '', '', '', WGSCoords)
        arcpy.management.AddField(outRoutesfc, "Name", "TEXT")
        spatial_ref = WGSCoords
    else:
        spatial_ref = arcpy.Describe(outRoutesfc).spatialReference


# ----- Create polylines using stops as vertices -----

    arcpy.AddMessage("- Generating polylines using stops as vertices")

    # Set up insertCursors for output shapes polylines and stop sequences
    # Have to open an edit session to have two simultaneous InsertCursors.

    edit = arcpy.da.Editor(outGDB)
    ucursor = arcpy.da.InsertCursor(outRoutesfc, ["SHAPE@", "Name"])
    cur = arcpy.da.InsertCursor(outSequencePoints, ["SHAPE@X", "SHAPE@Y", "shape_id", "sequence", "CurbApproach", "stop_id"])
    edit.startEditing()

    badStops = []

    for sequence in sequence_shape_dict:
        shape_id = sequence_shape_dict[sequence]
        route_id = sequence[0]
        route_type = RouteDict[route_id][4]
        if route_type in route_types_Straight or shape_id in NoRouteGenerated:
            sequence_num = 1
            # Add stop sequence to an Array of Points
            array = arcpy.Array()
            pt = arcpy.Point()
            for stop in sequence[1]:
                try:
                    stop_lat = stoplatlon_dict[stop][0]
                    stop_lon = stoplatlon_dict[stop][1]
                except KeyError:
                    if shape_id not in NoRouteGenerated:
                        # Don't repeat a warning if they already got it once.
                        badStops.append(stop)
                    sequence_num += 1
                    continue
                pt.X = float(stop_lon)
                pt.Y = float(stop_lat)
                # Add stop sequences to points fc for user to look at.
                cur.insertRow((float(stop_lon), float(stop_lat), shape_id, sequence_num, CurbApproach, stop))
                sequence_num = sequence_num + 1
                array.add(pt)
            # Generate a Polyline from the Array of stops
            polyline = arcpy.Polyline(array, WGSCoords)
            # Project the polyline to the correct output coordinate system.
            if spatial_ref != WGSCoords:
                polyline.projectAs(spatial_ref)
            # Add the polyline to the Shapes feature class
            ucursor.insertRow((polyline, shape_id))
    del ucursor
    del cur

    edit.stopEditing(True)

    if badStops:
        badStops = list(set(badStops))
        arcpy.AddWarning("Your stop_times.txt lists times for the following stops which are not included in your stops.txt file. These stops will be ignored. " + unicode(badStops))
