package BrakingSystem {

    part def Vehicle {
        attribute stoppingDistance : ISQ::LengthValue;
    }

    part vehicle : Vehicle {
        attribute :>> stoppingDistance = 45 [SI::m];
    }

    requirement def StoppingDistanceRequirement {
        doc /* The vehicle shall stop within 50 meters when braking. */
        subject veh : Vehicle;
        require constraint { veh.stoppingDistance <= 50 [SI::m] }
    }

}
