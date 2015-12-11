#!/usr/bin/env python
DESC="""EPP used to create running notes from the workset generation """

from argparse import ArgumentParser
from genologics.lims import Lims
from genologics.config import BASEURI,USERNAME,PASSWORD
from genologics.epp import attach_file, EppLogger
from genologics.entities import Process, Project
from datetime import datetime

import json
import sys


def main(lims, args):

    p=Process(lims, id=args.pid)
    log=[]
    datamap={}
    wsname=None
    for art in p.all_inputs():
        if len(art.samples)!=1:
            log.append("Warning : artifact {0} has more than one sample".format(art.id))
        for sample in art.samples:
           #take care of lamda DNA
           if sample.project:
                if sample.project.id not in datamap:
                    datamap[sample.project.id]=[sample.name]
                else:
                    datamap[sample.project.id].append(sample.name)

    for art in p.all_outputs():
        try:
            wsname=art.location[0].name
            break
        except:
            pass

    now=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    for pid in datamap:
        pj=Project(lims, id=pid)
        running_notes=json.loads(pj.udf['Running Notes'])
        if len(datamap[pid]) > 1:
            running_notes[now]="{0} samples planned for {1}".format(len(datamap[pid]), wsname)
        else:
            running_notes[now]="{0} sample planned for {1}".format(len(datamap[pid]), wsname)

        pj.udf['Running Notes']=json.dumps(running_notes)
        pj.put()
        log.append("Updated project {0} : {1}, {2} samples in this workset".format(pid,pj.name, datamap[pid]))


 
    with open("EPP_Notes.log", "w") as flog:
        flog.write("\n".join(log))
    for out in p.all_outputs():
        #attach the log file
        if out.name=="RNotes Log":
            attach_file(os.path.join(os.getcwd(), "EPP_Notes.log"), out)

if __name__=="__main__":
    parser = ArgumentParser(description=DESC)
    parser.add_argument('--pid',
                        help='Lims id for current Process')
    args = parser.parse_args()

    lims = Lims(BASEURI, USERNAME, PASSWORD)
    lims.check_version()
    main(lims, args)
