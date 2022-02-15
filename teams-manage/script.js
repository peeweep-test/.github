async function getTeamsNameList(github, context) {
  // console.log("getTeams");
  try {
    const teamsList = await github.rest.teams.list({
      org: context.payload.organization.login,
    });
    var teamsNameList = [];
    for (let teamsListMember of teamsList.data) {
      teamName = teamsListMember.name;
      teamsNameList.push(teamName);
    }
    return teamsNameList;
  } catch (e) {
    throw e;
  }
}

async function parseYaml() {
  // console.log("parseYaml");
  const yaml = require("js-yaml");
  const fs = require("fs");

  let teams;
  try {
    teams = yaml.load(fs.readFileSync("./teams.yaml", "utf-8")).teams;
    // console.log("%j", teams);

    return teams;
  } catch (e) {
    throw e;
  }
}

async function getTeamMembers(github, context, teamName) {
  try {
    const teamMembers = await github.rest.teams.listMembersInOrg({
      org: context.payload.organization.login,
      team_slug: teamName,
    });
    var teamMembersNamesList = [];
    if (teamMembers.data.length > 0) {
      // console.log("Team [%s] has %d members.", teamName, teamMembers.data.length);

      for (let teamMember of teamMembers.data) {
        // console.log("member: %s", teamMember.login);
        teamMembersNamesList.push(teamMember.login);
        // console.log(teamMember);
      }
    } else {
      // console.log("Team [%s] has no member.", teamName);
    }
    return teamMembersNamesList;
  } catch (e) {
    throw e;
  }
}

module.exports = async ({ github, context }) => {
  // parse yaml
  var teams = await parseYaml();
  // {"admin":{"members":["peeweep"],"repos":["all"]},"dtkcore":{"members":["peeweep"],"repos":["dtkcore"]}}
  console.log("yaml parse info: %j", teams);

  // get teams names
  teamsNameList = await getTeamsNameList(github, context);
  // console.log(teamsNameList);

  for (let team in teams) {
    // 如果team 不存在，新建team
    if (teamsNameList.indexOf(team) < 0) {
      console.log("Team [%s] doesn't exist, create this team.", team);
      try {
        const teamsCreate = await github.rest.teams.create({
          org: context.payload.organization.login,
          name: team,
          description: team + " Team",
        });
      } catch (e) {
        throw e;
      }
    } else {
      // console.log("Team [%s] already exist, skip create", team);
    }
    // console.log("Team [%s] yaml info: %j", team, teams[team]);

    var yamlMembers = teams[team].members;
    if (yamlMembers == null) {
      yamlMembers = [];
    }
    // console.log("Team [%s] yamlMembers: %j", team, yamlMembers);

    // 如果成员清单与team 中的不匹配，更新team 成员
    const realMembers = await getTeamMembers(github, context, team);
    // console.log("Team [%s] realMembers: %j", team, realMembers);

    // compare realTeamMembers and yamlTeamMembers
    var removeMembers = [];
    var addMembers = [];
    for (let realMember of realMembers) {
      if (yamlMembers.indexOf(realMember) < 0) {
        removeMembers.push(realMember);
      }
    }
    for (let yamlMember of yamlMembers) {
      if (realMembers.indexOf(yamlMember) < 0) {
        addMembers.push(yamlMember);
      }
    }
    // console.log("Team [%s] removeMembers: %j", team, removeMembers);
    // console.log("Team [%s] addMembers: %j", team, addMembers);

    // remove members
    for (let removeMember of removeMembers) {
      try {
        const removeMembership =
          await github.rest.teams.removeMembershipForUserInOrg({
            org: context.payload.organization.login,
            team_slug: team,
            username: removeMember,
          });
        console.log("Team [%s] remove Member [%s] success", team, removeMember);
      } catch (e) {
        throw e;
      }
    }

    // add or update members
    for (let addMember of addMembers) {
      try {
        const addOrUpdateMembership =
          await github.rest.teams.addOrUpdateMembershipForUserInOrg({
            org: context.payload.organization.login,
            team_slug: team,
            username: addMember,
            role: "member",
          });
        memberRole = addOrUpdateMembership.data.role;
        console.log(
          "Team [%s] add Member [%s] as Role [%s]",
          team,
          addMember,
          memberRole
        );
      } catch (e) {
        throw e;
      }
    }
  }
};
